---

**Frontmatter:**
- Title: "SLSA Supply Chain Security: Provenance, Attestations, and Build Integrity"
- Tags: `[devsecops, supply-chain-security, slsa, kubernetes, ci-cd]`
- Description: "How SLSA provenance and attestations close the gap between 'we signed the image' and 'we can prove exactly what built it.'"

**Post structure (~2,200 words):**
1. Opens with the 2023 PyPI typosquatting campaign (450+ malicious packages) as the hook
2. References the existing SVG diagram
3. **What SLSA Actually Is** — the 4 levels, and the critical L2 vs L3 distinction (who can forge provenance)
4. **Provenance Document Anatomy** — real in-toto/DSSE JSON with every field explained
5. **Generating Provenance in GitHub Actions** — `actions/attest-build-provenance` + `slsa-github-generator` for L3
6. **SBOMs as Attestations** — `syft` + `actions/attest-sbom`, and why CI-generated SBOMs beat local ones
7. **Verification commands** — `gh attestation verify`, `slsa-verifier`, `cosign verify-attestation` with real flags
8. **Kyverno Admission Policy** — full ClusterPolicy YAML enforcing provenance at deploy time
9. **Failure Modes** — mutable tags, SBOM drift, enforcement theater, hermetic build cost
10. **Migration Path** — Week 1 through Quarter 2 concrete steps