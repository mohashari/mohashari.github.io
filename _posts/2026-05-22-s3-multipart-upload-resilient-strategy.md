---
layout: post
title: "Designing a Resilient S3 Multipart Upload Strategy for Large Files"
date: 2026-05-22 08:00:00 +0700
tags: [aws, s3, storage, reliability, backend]
description: "How to build a highly reliable and performant file upload system using S3 Multipart Upload APIs with resume-on-failure capabilities."
image: "https://picsum.photos/seed/9466/1080/720"
thumbnail: "https://picsum.photos/seed/9466/400/300"
---

Handling file uploads is a routine requirement for backend developers. At low volumes and small file sizes (under 100MB), a simple, single-part HTTP `PUT` request directly to Amazon S3 (using presigned URLs) works flawlessly. 

However, as file sizes balloon into gigabytes—such as raw video footage, database backups, or scientific datasets—single-part uploads become highly vulnerable. A single packet loss or connection blip near the end of a 4GB upload forces the client to restart the entire transfer from scratch. Furthermore, S3 caps single-part uploads at exactly **5GB**.

To address these limitations, S3 offers the **Multipart Upload API**. In this technical blueprint, we'll examine the architecture of a resilient, parallelized, and resumeable S3 multipart upload system.

---

## 1. The Multipart Upload Architecture

An S3 Multipart Upload divides a single large file into a set of smaller, distinct chunks (ranging from **5MB** up to **5GB** per chunk, except for the last part which can be smaller). These chunks are uploaded independently and can be sent in parallel. 

```
                                  ┌───────────────────────────┐
                                  │   Initiate Upload (API)   │
                                  └─────────────┬─────────────┘
                                                │ (Returns Upload ID)
                                                ▼
                   ┌────────────────────────────────────────────────────────┐
                   ▼ (Part 1 - 10MB)         ▼ (Part 2 - 10MB)              ▼ (Part 3 - 5MB)
             ┌───────────┐             ┌───────────┐                  ┌───────────┐
             │ UploadPart│             │ UploadPart│                  │ UploadPart│
             └─────┬─────┘             └─────┬─────┘                  └─────┬─────┘
                   │ (ETag 1)                │ (ETag 2)                     │ (ETag 3)
                   └─────────────────────────┼──────────────────────────────┘
                                             ▼
                                  ┌───────────────────────────┐
                                  │    Complete Upload (API)  │
                                  └───────────────────────────┘
```

The upload workflow consists of three major phases:

1.  **Initiation:** The client asks the backend to initiate an upload. The backend calls S3's `CreateMultipartUpload`, which returns a unique **`UploadId`**.
2.  **Part Uploads:** The client splits the file and uploads each chunk (along with the `UploadId` and a sequential `PartNumber` from 1 to 10,000). For each successful part, S3 returns a unique header value called an **`ETag`**.
3.  **Completion:** Once all parts are uploaded, the client sends a completion request containing the `UploadId` and a sorted list of all `PartNumber`s and their corresponding `ETag`s. S3 then reconstructs the final file.

---

## 2. Architecting for Resilience: Key Strategies

### A. Parallelizing Part Uploads
Instead of uploading chunks sequentially, utilize a **Worker Pool** to upload multiple parts in parallel. This fully saturates the client's network bandwidth, dramatically reducing total upload times.

### B. Resume-on-Failure (State Tracking)
If a user's browser crashes or network drops, they shouldn't lose progress. To make the upload resumeable:
1. Save the `UploadId` and part boundaries in a persistent datastore (e.g., PostgreSQL).
2. When the user retries, query S3 using `ListParts` (or check your local database) to identify which `PartNumber`s were already uploaded successfully.
3. Skip the completed parts and only request/upload the missing ones.

### C. Preventing Orphaned Uploads (Billing Cleanup)
When a multipart upload is initiated, S3 reserves storage space for the incoming chunks. If an upload is abandoned halfway, **S3 continues to charge you for those stored, incomplete parts indefinitely.**

To prevent massive AWS bills, you **must** configure an **S3 Lifecycle Rule** on your bucket to automatically abort incomplete multipart uploads:

```json
// snippet-1
{
  "Rules": [
    {
      "ID": "AbortIncompleteMultipartUploadsAfter7Days",
      "Status": "Enabled",
      "Filter": {},
      "AbortIncompleteMultipartUpload": {
        "DaysAfterInitiation": 7
      }
    }
  ]
}
```

---

## 3. Implementation: Parallel Multipart Upload in Go

Here is a complete, concurrent implementation demonstrating how to initiate, upload chunks concurrently using goroutines, and complete an S3 multipart upload using the AWS SDK for Go v2.

```go
// snippet-2
package main

import (
	"context"
	"fmt"
	"io"
	"os"
	"sync"

	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/aws/aws-sdk-go-v2/config"
	"github.com/aws/aws-sdk-go-v2/service/s3"
	"github.com/aws/aws-sdk-go-v2/service/s3/types"
)

const (
	BucketName = "my-production-storage"
	ObjectKey  = "large-backups/db-backup-2026.tar.gz"
	ChunkSize  = 10 * 1024 * 1024 // 10MB Chunks
	NumWorkers = 5                // Parallel upload threads
)

type PartUploadJob struct {
	PartNumber int32
	Offset     int64
	Size       int64
}

type PartUploadResult struct {
	CompletedPart types.CompletedPart
	Err           error
}

func main() {
	ctx := context.TODO()
	cfg, err := config.LoadDefaultConfig(ctx, config.WithRegion("us-east-1"))
	if err != nil {
		panic(err)
	}
	s3Client := s3.NewFromConfig(cfg)

	file, err := os.Open("db-backup-2026.tar.gz")
	if err != nil {
		panic(err)
	}
	defer file.Close()

	fileInfo, _ := file.Stat()
	fileSize := fileInfo.Size()

	// 1. Initiate Multipart Upload
	initOutput, err := s3Client.CreateMultipartUpload(ctx, &s3.CreateMultipartUploadInput{
		Bucket: aws.String(BucketName),
		Key:    aws.String(ObjectKey),
	})
	if err != nil {
		panic(err)
	}
	uploadID := initOutput.UploadId
	fmt.Printf("Multipart Upload initiated. Upload ID: %s\n", *uploadID)

	// Calculate parts
	var parts []PartUploadJob
	var offset int64
	var partNum int32 = 1
	for offset < fileSize {
		size := int64(ChunkSize)
		if offset+size > fileSize {
			size = fileSize - offset
		}
		parts = append(parts, PartUploadJob{
			PartNumber: partNum,
			Offset:     offset,
			Size:       size,
		})
		offset += size
		partNum++
	}

	jobs := make(chan PartUploadJob, len(parts))
	results := make(chan PartUploadResult, len(parts))

	// Start Worker Pool
	var wg sync.WaitGroup
	for w := 1; w <= NumWorkers; w++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for job := range jobs {
				// Read chunk from file
				buffer := make([]byte, job.Size)
				_, readErr := file.ReadAt(buffer, job.Offset)
				if readErr != nil && readErr != io.EOF {
					results <- PartUploadResult{Err: readErr}
					continue
				}

				// Upload chunk
				partOutput, uploadErr := s3Client.UploadPart(ctx, &s3.UploadPartInput{
					Bucket:     aws.String(BucketName),
					Key:        aws.String(ObjectKey),
					UploadId:   uploadID,
					PartNumber: aws.Int32(job.PartNumber),
					Body:       io.NopCloser(io.NewSectionReader(file, job.Offset, job.Size)),
				})
				if uploadErr != nil {
					results <- PartUploadResult{Err: uploadErr}
					continue
				}

				results <- PartUploadResult{
					CompletedPart: types.CompletedPart{
						PartNumber: aws.Int32(job.PartNumber),
						ETag:       partOutput.ETag,
					},
				}
			}
		}()
	}

	// Enqueue jobs
	for _, job := range parts {
		jobs <- job
	}
	close(jobs)

	// Wait for workers in background
	go func() {
		wg.Wait()
		close(results)
	}()

	// Collect results
	var completedParts []types.CompletedPart
	for res := range results {
		if res.Err != nil {
			// Abort on any failure to clean S3
			_, _ = s3Client.AbortMultipartUpload(ctx, &s3.AbortMultipartUploadInput{
				Bucket:   aws.String(BucketName),
				Key:      aws.String(ObjectKey),
				UploadId: uploadID,
			})
			panic(fmt.Sprintf("Upload failed, transaction aborted: %v", res.Err))
		}
		completedParts = append(completedParts, res.CompletedPart)
	}

	// 3. Complete Multipart Upload
	_, err = s3Client.CompleteMultipartUpload(ctx, &s3.CompleteMultipartUploadInput{
		Bucket:   aws.String(BucketName),
		Key:      aws.String(ObjectKey),
		UploadId: uploadID,
		MultipartUpload: &types.CompletedMultipartUpload{
			Parts: completedParts,
		},
	})
	if err != nil {
		panic(err)
	}
	fmt.Println("Multipart Upload completed successfully!")
}
```

---

## Summary

Moving away from single-part uploads for files larger than 100MB is a necessity in production systems. By structuring a concurrent S3 multipart upload system backed by automatic lifecycle cleanup and proper part-level state tracking, you can guarantee fast, reliable, and fault-tolerant transfers even over volatile networks.
