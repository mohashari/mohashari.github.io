---
layout: post
title: "Implementing Cryptographic Attestations for Multi-Tenant Database Queries Using KMS Envelope Encryption"
date: 2026-08-11 08:00:00 +0700
tags: [cryptography, multi-tenancy, aws-kms, go, devsecops]
description: "Enforce mathematical boundaries over logical ones. Learn how to combine AWS KMS, Nitro Enclaves, and query-bound JWTs to prevent cross-tenant data leaks."
image: "https://picsum.photos/seed/7665/1080/720"
thumbnail: "https://picsum.photos/seed/7665/400/300"
---

In a typical multi-tenant SaaS application, logical separation is the default defense. Developers write a query, append `WHERE tenant_id = ?`, and trust their ORM or PostgreSQL Row-Level Security (RLS) to enforce isolation. But in production, logical boundaries are fragile. A single developer omitting a where-clause in a complex JOIN, an ORM eager-loading edge case, or a connection pooler like PgBouncer in transaction mode mismanaging session variables can instantly expose Tenant A's private data to Tenant B. For enterprise clients, logical isolation is no longer enough to satisfy compliance. To build a system that is resilient against application-level compromises and developer errors, you must shift from logical boundaries to cryptographic ones. This post details how to implement a production-grade, zero-trust data access pipeline using AWS KMS envelope encryption, secure Nitro Enclaves, and cryptographically signed query attestations.

## The Fallacy of Logical Multi-Tenancy

Logical isolation assumes that your application code is correct and that the runtime environment is secure. Both assumptions fail under pressure. In high-throughput PostgreSQL environments, using session-bound variables to enforce RLS (e.g., `SET LOCAL app.current_tenant = 'tenant-uuid'`) is notorious for leaks when combined with transaction-level pooling. If the pooler reuses a connection before the session state is reliably cleared, subsequent queries execute in the context of the previous tenant. 

Furthermore, if an attacker achieves Remote Code Execution (RCE) on your application server, they bypass RLS completely by accessing the underlying database credentials. If those credentials allow global read access to the tables, the attacker can dump the entire database.

Cryptographic multi-tenancy addresses this by encrypting each tenant's data with a unique Data Encryption Key (DEK). The DEK is encrypted (wrapped) by a tenant-specific Key Encryption Key (KEK) managed by a Hardware Security Module (HSM) inside AWS KMS. However, naive envelope encryption still leaves a gap: if the application server is compromised, it can simply call the KMS API to decrypt any tenant's DEK. 

To solve this, we must decouple database access from decryption capability. The application server executes SQL queries but cannot decrypt the data. The decryption is delegated to an isolated, secure environment—a Nitro Enclave—which only releases the plaintext if the application presents a valid, cryptographically signed *Tenant Attestation Token* that binds the request to the specific database query.

## The Secure Enclave Architecture

AWS Nitro Enclaves provide isolated, virtual machines with no persistent storage, no interactive operator access (no SSH, no root shell), and a secure communication channel (virtual sockets or `vsock`) connected to the host EC2 instance. 

The security of this model relies on a clean separation of concerns:

1. **The Identity Provider (IdP):** A trusted auth service generates a short-lived, RSA-signed Tenant Attestation Token (JWT) when a user authenticates. This token contains the tenant ID and a SHA-256 hash of the specific SQL query the application is authorized to run.
2. **The Application Service:** Queries PostgreSQL to retrieve the encrypted row, which contains the ciphertext, the initialization vector (nonce), and the wrapped DEK. It then sends this payload, along with the JWT and the raw SQL query, to the Nitro Enclave over `vsock`.
3. **The Cryptographic Enclave Agent:** Runs inside the enclave. It verifies the JWT signature, matches the tenant ID, and validates that the query hash in the token matches the SHA-256 of the incoming SQL query.
4. **AWS KMS:** Decrypts the DEK. The key policy on the KEK restricts the `kms:Decrypt` action to the enclave's IAM role, and crucially, evaluates the Enclave's cryptographic measurements (Platform Configuration Registers or PCRs) and the `kms:EncryptionContext`.

If any component of this chain is altered—for example, if an attacker alters the enclave code to bypass JWT verification—the enclave's PCR0 measurement changes, and AWS KMS will reject all decryption calls.

## Database Layout & Data Schema

To support row-level envelope encryption, our database schema must store the cryptographic metadata required to decrypt the payload alongside the ciphertext. 

```sql
-- // snippet-1
CREATE TABLE tenant_secrets (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL,
    encrypted_data BYTEA NOT NULL,      -- The AES-GCM ciphertext payload
    nonce BYTEA NOT NULL,               -- AES-GCM Initialization Vector (12 bytes)
    wrapped_dek BYTEA NOT NULL,         -- Data Encryption Key (DEK) encrypted by KEK
    kek_key_arn VARCHAR(2048) NOT NULL, -- The specific tenant's KMS KEK ARN
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_tenant_secrets_tenant_id ON tenant_secrets(tenant_id);
```

By storing `wrapped_dek` and `kek_key_arn` inside the row, we avoid global key management issues. Every row is self-contained. The database administrator can view the table, but the raw secrets remain protected by the AES-GCM layer.

## Generating the Attestation Token

To prevent an application server from using a compromised tenant token to decrypt unauthorized data, the auth service must bind the token to the specific database operation. We achieve this by embedding the SHA-256 hash of the target SQL query in the token claims under the `qhs` key.

```go
// // snippet-2
package attestation

import (
	"crypto/rsa"
	"crypto/sha256"
	"encoding/hex"
	"time"

	"github.com/golang-jwt/jwt/v5"
)

type TenantClaims struct {
	TenantID  string `json:"tid"`
	QueryHash string `json:"qhs"`
	jwt.RegisteredClaims
}

func GenerateAttestationToken(tenantID string, query string, privateKey *rsa.PrivateKey, ttl time.Duration) (string, error) {
	hasher := sha256.New()
	hasher.Write([]byte(query))
	queryHash := hex.EncodeToString(hasher.Sum(nil))

	claims := TenantClaims{
		TenantID:  tenantID,
		QueryHash: queryHash,
		RegisteredClaims: jwt.RegisteredClaims{
			Issuer:    "auth-service.prod.internal",
			Subject:   "app-service-client",
			Audience:  jwt.ClaimStrings{"cryptographic-attestation-proxy"},
			ExpiresAt: jwt.NewNumericDate(time.Now().Add(ttl)),
			IssuedAt:  jwt.NewNumericDate(time.Now()),
		},
	}

	token := jwt.NewWithClaims(jwt.SigningMethodRS256, claims)
	return token.SignedString(privateKey)
}
```

The resulting token is short-lived (e.g., 30 seconds). Even if an attacker intercepts this token, they can only use it to decrypt data returned by the exact SQL query it was generated for.

## Implementing the Enclave Decryption Agent

The agent running inside the Nitro Enclave is the core policy enforcement engine. It is written in Go and exposes a socket server over `vsock`. It accepts the encrypted payload, verifies the attestation token against the identity provider's public key, and calls KMS to decrypt the DEK.

```go
// // snippet-3
package enclave

import (
	"context"
	"crypto/aes"
	"crypto/cipher"
	"crypto/rsa"
	"crypto/sha256"
	"encoding/hex"
	"fmt"

	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/aws/aws-sdk-go-v2/service/kms"
	"github.com/golang-jwt/jwt/v5"
)

type DecryptRequest struct {
	EncryptedData []byte `json:"encrypted_data"`
	Nonce         []byte `json:"nonce"`
	WrappedDEK    []byte `json:"wrapped_dek"`
	KekKeyARN     string `json:"kek_key_arn"`
	TenantID      string `json:"tenant_id"`
	SQLQuery      string `json:"sql_query"`
	Token         string `json:"token"`
}

type TenantClaims struct {
	TenantID  string `json:"tid"`
	QueryHash string `json:"qhs"`
	jwt.RegisteredClaims
}

type Decryptor struct {
	kmsClient *kms.Client
	verifyKey *rsa.PublicKey
}

func NewDecryptor(kmsClient *kms.Client, verifyKey *rsa.PublicKey) *Decryptor {
	return &Decryptor{
		kmsClient: kmsClient,
		verifyKey: verifyKey,
	}
}

func (d *Decryptor) DecryptRow(ctx context.Context, req DecryptRequest) ([]byte, error) {
	// 1. Verify and parse the cryptographic attestation token
	token, err := jwt.ParseWithClaims(req.Token, &TenantClaims{}, func(token *jwt.Token) (interface{}, error) {
		if _, ok := token.Method.(*jwt.SigningMethodRSA); !ok {
			return nil, fmt.Errorf("unexpected signing method: %v", token.Header["alg"])
		}
		return d.verifyKey, nil
	})
	if err != nil || !token.Valid {
		return nil, fmt.Errorf("invalid attestation token: %w", err)
	}

	claims, ok := token.Claims.(*TenantClaims)
	if !ok {
		return nil, fmt.Errorf("invalid claims schema")
	}

	// 2. Enforce Tenant ID boundary matching
	if claims.TenantID != req.TenantID {
		return nil, fmt.Errorf("tenant ID mismatch: claim=%s, query=%s", claims.TenantID, req.TenantID)
	}

	// 3. Prevent token reuse across unauthorized queries
	hasher := sha256.New()
	hasher.Write([]byte(req.SQLQuery))
	actualQueryHash := hex.EncodeToString(hasher.Sum(nil))
	if claims.QueryHash != actualQueryHash {
		return nil, fmt.Errorf("query binding verification failed")
	}

	// 4. Decrypt the wrapped DEK using KMS, passing tenant context
	kmsOutput, err := d.kmsClient.Decrypt(ctx, &kms.DecryptInput{
		CiphertextBlob: req.WrappedDEK,
		KeyId:          aws.String(req.KekKeyARN),
		EncryptionContext: map[string]string{
			"tenant_id": req.TenantID,
		},
	})
	if err != nil {
		return nil, fmt.Errorf("kms decryption failed: %w", err)
	}

	// 5. Decrypt the row content in-memory using AES-GCM
	block, err := aes.NewCipher(kmsOutput.Plaintext)
	if err != nil {
		return nil, fmt.Errorf("cipher initialization error: %w", err)
	}

	aesGCM, err := cipher.NewGCM(block)
	if err != nil {
		return nil, fmt.Errorf("gcm instantiation error: %w", err)
	}

	plaintext, err := aesGCM.Open(nil, req.Nonce, req.EncryptedData, nil)
	if err != nil {
		return nil, fmt.Errorf("data payload decryption failed: %w", err)
	}

	return plaintext, nil
}
```

Notice the `EncryptionContext` parameter passed to the KMS call. This is not a secret, but a non-secret key-value pair cryptographically bound to the ciphertext. If the tenant ID in the request does not match the one used during encryption, KMS will reject the decryption request, even if the key is correct.

## KMS Key Policy Integration

To guarantee that only our verified enclave can execute decrypt operations, we configure the KMS Key Policy to validate both the IAM caller identity and the enclave image measurement (PCR0).

```json
// // snippet-4
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "AllowEnclaveDecryptOnlyForSpecificTenant",
      "Effect": "Allow",
      "Principal": {
        "AWS": "arn:aws:iam::111122223333:role/cryptographic-attestation-proxy-role"
      },
      "Action": "kms:Decrypt",
      "Resource": "*",
      "Condition": {
        "StringEquals": {
          "kms:EncryptionContext:tenant_id": "tenant-a-uuid-12345",
          "kms:RecipientAttestation:PCR0": "9491b489a242a420b925b364805e54c86121db5976b7db68565fb078b665dfb1e8e6b1856cfbe9e6349c25f187a54aef"
        }
      }
    }
  ]
}
```

The AWS KMS service checks the attestation document generated by the Nitro Enclave hypervisor during the TLS handshake. If the code inside the enclave is modified, PCR0 will change, rendering the decryption role useless.

## Application Client Orchestration

The application server queries the database, marshals the database fields, and executes a socket connection to the enclave via vsock.

```go
// // snippet-5
package app

import (
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
	"time"

	"github.com/mdlayher/vsock"
)

type TenantSecretRow struct {
	TenantID      string
	EncryptedData []byte
	Nonce         []byte
	WrappedDEK    []byte
	KekKeyARN     string
}

type DecryptRequest struct {
	EncryptedData []byte `json:"encrypted_data"`
	Nonce         []byte `json:"nonce"`
	WrappedDEK    []byte `json:"wrapped_dek"`
	KekKeyARN     string `json:"kek_key_arn"`
	TenantID      string `json:"tenant_id"`
	SQLQuery      string `json:"sql_query"`
	Token         string `json:"token"`
}

func FetchAndDecryptSecret(ctx context.Context, db *sql.DB, enclaveCID uint32, enclavePort uint32, tenantID string, secretID string, attestationToken string) ([]byte, error) {
	query := "SELECT tenant_id, encrypted_data, nonce, wrapped_dek, kek_key_arn FROM tenant_secrets WHERE id = $1 AND tenant_id = $2"
	
	var row TenantSecretRow
	err := db.QueryRowContext(ctx, query, secretID, tenantID).Scan(
		&row.TenantID,
		&row.EncryptedData,
		&row.Nonce,
		&row.WrappedDEK,
		&row.KekKeyARN,
	)
	if err != nil {
		return nil, fmt.Errorf("database query failed: %w", err)
	}

	req := DecryptRequest{
		EncryptedData: row.EncryptedData,
		Nonce:         row.Nonce,
		WrappedDEK:    row.WrappedDEK,
		KekKeyARN:     row.KekKeyARN,
		TenantID:      row.TenantID,
		SQLQuery:      query,
		Token:         attestationToken,
	}

	reqBytes, err := json.Marshal(req)
	if err != nil {
		return nil, fmt.Errorf("failed to marshal enclave request: %w", err)
	}

	conn, err := vsock.Dial(enclaveCID, enclavePort, nil)
	if err != nil {
		return nil, fmt.Errorf("failed to connect to enclave vsock: %w", err)
	}
	defer conn.Close()

	if err := conn.SetDeadline(time.Now().Add(5 * time.Second)); err != nil {
		return nil, fmt.Errorf("failed to set vsock deadline: %w", err)
	}

	if _, err := conn.Write(reqBytes); err != nil {
		return nil, fmt.Errorf("failed to write payload to enclave: %w", err)
	}

	buf := make([]byte, 4096)
	n, err := conn.Read(buf)
	if err != nil {
		return nil, fmt.Errorf("failed to read response from enclave: %w", err)
	}

	return buf[:n], nil
}
```

## Compilation and Packaging Pipeline

To package our Go enclave agent into an Enclave Image File (EIF) and extract its cryptographic measurements, we use `nitro-cli` running on our CI/CD runner.

```bash
// // snippet-6
#!/usr/bin/env bash
set -euo pipefail

# Build the Go cryptographic agent for Linux target
GOOS=linux GOARCH=amd64 CGO_ENABLED=0 go build -ldflags="-s -w" -o ./bin/enclave-agent ./cmd/enclave

# Build the docker image
docker build -t cryptographic-enclave-agent:latest -f Enclave.dockerfile .

# Generate the Enclave Image File (EIF) and extract PCR hashes for attestation
nitro-cli build-enclave \
    --docker-uri cryptographic-enclave-agent:latest \
    --output-file cryptographic-enclave-agent.eif

# Print the cryptographic measurements (PCR0, PCR1, PCR2)
echo "Enclave successfully built. PCR measurements:"
jq .Measurements cryptographic-enclave-agent.eif
```

The underlying `Enclave.dockerfile` packages the static binary on top of a minimal alpine base:

```dockerfile
// // snippet-7
FROM alpine:3.18

RUN apk add --no-cache ca-certificates

COPY bin/enclave-agent /usr/local/bin/enclave-agent

# Enclave communicates via vsock. Port 5005 is exposed inside the enclave.
ENTRYPOINT ["/usr/local/bin/enclave-agent", "--vsock-port", "5005"]
```

## Ephemeral DEK Caching

Envelope encryption carries a severe performance penalty. A single row decryption requires a roundtrip to AWS KMS, which introduces 15–40 milliseconds of latency. For batch queries, this scale of overhead is prohibitive. 

To mitigate this, we cache decrypted DEKs inside the Nitro Enclave's in-memory space. Caching inside the enclave is safe: the enclave's memory is cryptographically isolated from the host operating system by the AMD SEV/Intel SGX memory encryption hardware, meaning even a root user on the host EC2 instance cannot dump the cache contents.

```go
// // snippet-8
package enclave

import (
	"sync"
	"time"
)

type cachedDEK struct {
	rawKey    []byte
	expiresAt time.Time
}

type DEKCache struct {
	sync.RWMutex
	store map[string]cachedDEK
}

func NewDEKCache() *DEKCache {
	return &DEKCache{
		store: make(map[string]cachedDEK),
	}
}

func (c *DEKCache) Get(wrappedDEKHash string) ([]byte, bool) {
	c.RLock()
	defer c.RUnlock()

	item, exists := c.store[wrappedDEKHash]
	if !exists || time.Now().After(item.expiresAt) {
		return nil, false
	}
	return item.rawKey, true
}

func (c *DEKCache) Set(wrappedDEKHash string, rawKey []byte, ttl time.Duration) {
	c.Lock()
	defer c.Unlock()

	c.store[wrappedDEKHash] = cachedDEK{
		rawKey:    rawKey,
		expiresAt: time.Now().Add(ttl),
	}
}
```

The cache uses the hash of the `wrapped_dek` as its key, bypassing KMS calls for rows that share the same key within the TTL.

## Failure Modes & Operational Realities

Deploying cryptographic attestations at scale requires planning for specific runtime failure modes:

*   **KMS Throttling:** AWS KMS accounts have default limits on cryptographic operations (typically 10,000 requests/second per region). If your cache misses spike, your application will receive `ThrottlingException` errors. You must implement client-side exponential backoff with jitter and dynamically tune your DEK cache TTL based on query access patterns.
*   **Vsock Buffer Exhaustion:** The virtual socket (`vsock`) interface is fast but has limited buffer sizes compared to TCP. Highly concurrent applications writing large payloads to the enclave can cause vsock hangs. Optimize connection pools and implement a timeout pattern in your Go dialer to prevent blockages.
*   **Key Policy Lockout:** If you build your enclave CI/CD pipeline without automating the update of the KMS key policy, a code change will generate a new PCR0 hash and lock your application out of the data. Your deployment pipeline must dynamically update the key policies using infrastructure-as-code (Terraform or CloudFormation) before launching the new enclave image version.

By shifting tenant isolation from the database query layer to a cryptographically attested enclave pipeline, you eliminate entire classes of multi-tenant vulnerabilities, protecting your customers' data even in the event of an application compromise.