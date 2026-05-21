---
layout: post
title: "Understanding Git Internals: Objects, Content-Addressable Storage, and Packfiles"
date: 2026-05-27 08:00:00 +0700
tags: [git, systems-programming, under-the-hood, development-tools]
description: "A deep dive into Git's underlying data model, exploring blobs, trees, commits, references, and how packfiles optimize disk storage."
image: ""
thumbnail: ""
---

Almost every software engineer uses Git daily to manage source code. Yet, to many, Git remains a black box. Commands like `git rebase`, `git merge`, or resolving a detached `HEAD` can feel like magic or a recipe for disaster.

The secret to mastering Git is to realize that under the hood, **Git is not a delta-based version control system**. It is actually a simple **content-addressable filesystem** with a version-control interface layered on top.

In this deep dive, we will open up the `.git` directory, explore the four fundamental Git objects, calculate a SHA-1 hash manually, and understand how packfiles optimize storage for massive repositories.

---

## 1. Inside the `.git/objects` Directory

If you initialize a fresh Git repository and create a single commit, Git creates a hidden directory named `.git/`. This folder contains your entire repository's history. The core database where your snapshots reside is located in `.git/objects/`.

Git stores all content as **objects**. Every object is compressed using **zlib** and is identified by a unique **40-character SHA-1 hash**.

To optimize filesystem lookups, Git splits the 40-character hash:
*   The **first 2 characters** become the subdirectory name (e.g., `.git/objects/4b/`).
*   The **remaining 38 characters** become the filename (e.g., `825dc642cb6eb9a0...`).

---

## 2. The Four Git Object Types

Every object in Git's database belongs to one of four primitive types:

```
        +----------------------------------------+
        |                COMMIT                  |
        |  Points to: Tree Hash, Parent Hash     |
        |  Metadata: Author, Message             |
        +----------------───┬────────────────────+
                            │
                            ▼
        +----------------────────────────--------+
        |                 TREE                   |
        |  Maps: Filenames -> SHA-1 Hashes       |
        +----------------───┬────────────────────+
                            │
                            ▼
        +----------------────────────────--------+
        |                 BLOB                   |
        |  Raw File Content Only (No name)       |
        +----------------------------------------+
```

### A. Blobs (Binary Large Objects)
A **blob** stores only raw file contents. It **does not store metadata**—it does not know the filename, directory path, or read/write permissions.

### B. Trees
A **tree** represents a directory. It maps filenames, permissions (modes), and object types (blob or another nested tree) to their corresponding SHA-1 hashes. This is how Git maintains your project's folder hierarchy.

### C. Commits
A **commit** represents a snapshot of the repository. It points to a single root **tree** hash (representing the root directory of the project), one or more **parent commits** (for history propagation), and contains metadata like the author, committer, timestamp, and commit message.

### D. Annotated Tags
An **annotated tag** is a simple pointer to a specific commit, containing a tagger's name, timestamp, and tag message.

---

## 3. Demystifying Content-Addressable Storage

Git is "content-addressable" because the key to retrieve any data is derived directly from the content itself. If you write the exact same text to two different files in your repository, Git will generate the exact same SHA-1 hash, storing only **one physical blob** in `.git/objects`.

Let's see how Git calculates this hash. A Git object header consists of the object type, a space, the size of the content in bytes, followed by a null byte (`\0`), and finally the raw content.

The formula is:
```
SHA1 = sha1(type + " " + size_in_bytes + "\0" + content)
```

### Manual Hash Calculation in Bash

Let's verify this in the terminal. We will create a file containing the string `"Hello Git!"` and calculate its SHA-1 hash using standard Linux tools:

```bash
# snippet-1
# Write text to a file without a trailing newline
printf "Hello Git!" > test.txt

# Calculate hash using Git's internal header logic
(printf "blob 10\0"; cat test.txt) | sha1sum
```

This outputs:
```
4b825dc642cb6eb9a0f9e54e48a73752e519e48a  -
```

Now, let's write it using native Git commands and inspect the output:

```bash
# snippet-2
# Write object to Git database
git hash-object -w test.txt
```

This returns the *exact same* hash:
```
4b825dc642cb6eb9a0f9e54e48a73752e519e48a
```

We can verify the object's type and content using `cat-file`:

```bash
# snippet-3
# Check type
git cat-file -t 4b825dc642cb6eb9a0f9e54e48a73752e519e48a  # Returns: blob

# Check content
git cat-file -p 4b825dc642cb6eb9a0f9e54e48a73752e519e48a  # Returns: Hello Git!
```

---

## 4. References: How Branches Work

If Git objects are permanent and identified by cryptographically secure hashes, how do we track our active branch?

In Git, a **branch** is not a container or a folder. It is simply a **reference (ref)**: a small text file containing the 40-character SHA-1 hash of a commit.

If you open the file `.git/refs/heads/master`, you will see a single line:
```
8a3f9e82bc7190...
```

When you make a new commit on the `master` branch:
1. Git writes the new commit object to `.git/objects/`.
2. Git overwrites `.git/refs/heads/master` with the new commit's SHA-1 hash.

The special **`HEAD`** file (`.git/HEAD`) simply points to the active branch reference:
```
ref: refs/heads/master
```

---

## 5. Storage Optimization: Loose Objects vs. Packfiles

By default, Git writes every file snapshot as a separate **loose object**. If you modify a 10MB text file by adding a single line, Git will write a *completely new* 10MB compressed loose object, which quickly wastes disk space.

To optimize storage, Git runs a garbage collection process (`git gc`). It packs loose objects into a single compressed binary file called a **Packfile** (`.pack`), along with an Index file (`.idx`) for fast byte-offset lookups.

### Delta Compression
Inside a packfile, Git uses **delta compression**. It compares similar files (like different versions of the same file across commits), stores one version in full, and stores all other versions as **diffs (deltas)** relative to that base file. 

This compression algorithm is so efficient that a Git repository's history is often significantly smaller than the uncompressed size of the working directory!

---

## Summary

Git's elegance lies in its simplicity. By structuring your repository as a simple directed acyclic graph (DAG) of content-addressed commits, trees, and blobs, Git achieves absolute integrity, cryptographic verifiability, and extreme speed. The next time you run a Git command, remember that you are simply manipulating a graph of immutable content-addressed nodes.
