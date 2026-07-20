---
layout: post
title: "Implementing a Custom Storage Engine in Go: LSM-Trees, Memtables, and SSTable Merging"
date: 2026-06-20 08:00:00 +0700
tags: [go, database, software-engineering, storage]
description: "Build a high-performance LSM-tree storage engine in Go. Learn key design patterns, from write-ahead logging and SkipLists to Bloom filters and multiway merge compaction."
image: ""
thumbnail: ""
---

At scale, traditional B-tree-based relational and key-value databases fall off a performance cliff under write-heavy workloads. As concurrent insertions and updates trigger random disk writes, split-leaf disk pages, and deep lock contention inside the engine, write throughput degrades from tens of thousands of writes per second to a crawl. In modern high-throughput environments—such as time-series metrics ingestion, distributed event streaming buffers, or real-time tracking systems—we need a storage architecture optimized for sequential write speeds. This is where Log-Structured Merge-Trees (LSM-trees) shine. By transforming random writes into sequential memory inserts and batch-flushing them to disk as immutable sorted tables, LSM engines bypass the B-tree write bottleneck. However, this write speed comes at a cost: read path amplification, file descriptor churn, memory overhead, and unpredictable background compaction pauses. This post breaks down how to implement a custom, production-grade LSM-tree storage engine in Go, optimizing memory overhead, disk structure, and the concurrent merging pipelines.

![Implementing a Custom Storage Engine in Go: LSM-Trees, Memtables, and SSTable Merging Diagram](/images/diagrams/implementing-lsm-tree-storage-engine-go-memtable-sstable.svg)

## The Architecture of an LSM-Tree Storage Engine

An LSM-tree storage engine decouples the write path from the read path by structuring data into multiple components across memory and disk. The write path is strictly append-only. When a client performs a `Put(key, value)` operation, the engine writes the entry to an append-only Write-Ahead Log (WAL) on disk to guarantee durability. Simultaneously, it inserts the record into a memory-resident sorted structure called the Memtable. 

Because the Memtable is always sorted, search operations in memory are extremely fast. However, memory is finite. When the Memtable reaches a pre-configured size threshold—typically 32MB or 64MB in production systems—it is marked as immutable, and a new active Memtable is spawned to accept incoming writes. A background flusher thread then serializes the immutable Memtable to disk as a Level 0 (L0) Sorted String Table (SSTable). 

Once written to disk, SSTables are immutable. Because L0 SSTables are flushed directly from memory at different times, their key ranges overlap. As more L0 SSTables accumulate, search latency degrades because a read query might have to search every single L0 file. To resolve this, a background compaction process periodically merges multiple SSTables using a multiway merge-sort algorithm, removing duplicate keys and deletion markers (tombstones) while moving the consolidated data to lower levels (Level 1, Level 2, etc.) where key ranges are strictly non-overlapping.

## Designing a Memory-Efficient Memtable with SkipLists

The Memtable must support fast, concurrent reads, writes, and in-order scans. While a balanced binary search tree (like a Red-Black tree) is a common choice, its rebalancing phases under high concurrency require complex locking schemes. A SkipList provides a probabilistic alternative with $O(\log n)$ search, insertion, and deletion complexities. It utilizes a layered hierarchy of forward pointers, allowing queries to skip large sections of the list.

However, implementing a SkipList in Go poses a major threat to garbage collection (GC) performance. Go's runtime GC is a concurrent mark-and-sweep collector. It scans the heap by tracing pointers. If our Memtable stores millions of small key-value records using naive pointer-heavy nodes, the GC will spend milliseconds scanning these pointers, leading to latency spikes. To combat this, we must track the exact byte allocations of our Memtable, enabling the engine to freeze the Memtable before it exceeds bounds and triggers heap fragmentation.

```go
// snippet-1
package storage

import (
	"bytes"
	"math/rand"
	"sync"
	"sync/atomic"
)

const MaxHeight = 12

type skipNode struct {
	key       []byte
	val       []byte
	ts        int64
	tombstone bool
	forward   []*skipNode
}

// Memtable manages sorted data in-memory and tracks exact size in bytes to prevent OOMs.
type Memtable struct {
	mu          sync.RWMutex
	head        *skipNode
	height      int
	sizeInBytes int64
}

func NewMemtable() *Memtable {
	return &Memtable{
		head: &skipNode{
			forward: make([]*skipNode, MaxHeight),
		},
		height: 1,
	}
}

func compareKeys(a, b []byte) int {
	return bytes.Compare(a, b)
}

func (m *Memtable) randomHeight() int {
	h := 1
	for h < MaxHeight && rand.Float32() < 0.5 {
		h++
	}
	return h
}

func (m *Memtable) Put(key, val []byte, ts int64, tombstone bool) {
	m.mu.Lock()
	defer m.mu.Unlock()

	update := make([]*skipNode, MaxHeight)
	curr := m.head

	for i := m.height - 1; i >= 0; i-- {
		for curr.forward[i] != nil && compareKeys(curr.forward[i].key, key) < 0 {
			curr = curr.forward[i]
		}
		update[i] = curr
	}

	curr = curr.forward[0]
	if curr != nil && compareKeys(curr.key, key) == 0 {
		// Overwrite value and update size metrics
		oldLen := len(curr.val)
		curr.val = val
		curr.ts = ts
		curr.tombstone = tombstone
		atomic.AddInt64(&m.sizeInBytes, int64(len(val)-oldLen))
		return
	}

	// Insert new node at a random height level
	level := m.randomHeight()
	if level > m.height {
		for i := m.height; i < level; i++ {
			update[i] = m.head
		}
		m.height = level
	}

	newNode := &skipNode{
		key:       key,
		val:       val,
		ts:        ts,
		tombstone: tombstone,
		forward:   make([]*skipNode, level),
	}

	for i := 0; i < level; i++ {
		newNode.forward[i] = update[i].forward[i]
		update[i].forward[i] = newNode
	}

	// Approximate heap size: key + val + node struct overhead (32 bytes) + pointers
	nodeSize := int64(len(key) + len(val) + 32 + level*8)
	atomic.AddInt64(&m.sizeInBytes, nodeSize)
}

func (m *Memtable) Get(key []byte) (skipNode, bool) {
	m.mu.RLock()
	defer m.mu.RUnlock()

	curr := m.head
	for i := m.height - 1; i >= 0; i-- {
		for curr.forward[i] != nil && compareKeys(curr.forward[i].key, key) < 0 {
			curr = curr.forward[i]
		}
	}

	curr = curr.forward[0]
	if curr != nil && compareKeys(curr.key, key) == 0 {
		return *curr, true
	}
	return skipNode{}, false
}

func (m *Memtable) Size() int64 {
	return atomic.LoadInt64(&m.sizeInBytes)
}
```

## Write-Ahead Logging (WAL) for Crash Recovery

Because the Memtable resides in volatile memory, any power loss or process crash will result in data loss. To satisfy durability requirements, writes must be committed to a Write-Ahead Log (WAL) on disk before updating the Memtable. 

The WAL is an append-only file. Each log entry is structured with a length prefix, a timestamp, a tombstone boolean, and a CRC32 checksum. If the system crashes mid-write, the database parses the WAL during bootstrap, recalculates checksums, and restores the Memtable to its pre-crash state. 

However, calling a disk write for every single entry introduces massive disk I/O bottlenecks. In production systems, we use an OS-level file cache, writing updates using a buffered channel, and periodically executing `fsync` (either on a timer like every 10ms, or when the buffer reaches a threshold) to flush the kernel page cache to physical storage.

```go
// snippet-2
package storage

import (
	"encoding/binary"
	"errors"
	"hash/crc32"
	"io"
	"os"
)

var ErrChecksumMismatch = errors.New("wal record corrupted: crc32 checksum mismatch")

// WAL handles append-only logging of mutations before memtable insertion.
type WAL struct {
	file *os.File
}

func OpenWAL(path string) (*WAL, error) {
	file, err := os.OpenFile(path, os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0644)
	if err != nil {
		return nil, err
	}
	return &WAL{file: file}, nil
}

// Append writes a key-value record with a timestamp, tombstone flag, and CRC32 checksum.
func (w *WAL) Append(key, value []byte, ts int64, tombstone bool) error {
	// Record Layout:
	// | CRC32 (4B) | Timestamp (8B) | Tombstone (1B) | KeyLen (4B) | Key (var) | ValLen (4B) | Val (var) |
	keyLen := uint32(len(key))
	valLen := uint32(len(value))
	recordSize := 4 + 8 + 1 + 4 + keyLen + 4 + valLen
	buf := make([]byte, recordSize)

	// Build payload (starting at byte index 4, saving room for CRC)
	binary.BigEndian.PutUint64(buf[4:12], uint64(ts))
	if tombstone {
		buf[12] = 1
	} else {
		buf[12] = 0
	}
	binary.BigEndian.PutUint32(buf[13:17], keyLen)
	copy(buf[17:17+keyLen], key)
	
	valStart := 17 + keyLen
	binary.BigEndian.PutUint32(buf[valStart:valStart+4], valLen)
	copy(buf[valStart+4:], value)

	// Compute checksum of the data payload
	checksum := crc32.ChecksumIEEE(buf[4:])
	binary.BigEndian.PutUint32(buf[0:4], checksum)

	_, err := w.file.Write(buf)
	return err
}

func (w *WAL) Sync() error {
	return w.file.Sync()
}

func (w *WAL) Close() error {
	return w.file.Close()
}

type WALReader struct {
	file *os.File
}

func NewWALReader(path string) (*WALReader, error) {
	file, err := os.OpenFile(path, os.O_RDONLY, 0644)
	if err != nil {
		return nil, err
	}
	return &WALReader{file: file}, nil
}

func (r *WALReader) ReadNext() ([]byte, []byte, int64, bool, error) {
	header := make([]byte, 17) // Checksum (4B) + TS (8B) + Tombstone (1B) + KeyLen (4B)
	if _, err := io.ReadFull(r.file, header); err != nil {
		return nil, nil, 0, false, err
	}

	checksum := binary.BigEndian.Uint32(header[0:4])
	ts := int64(binary.BigEndian.Uint64(header[4:12]))
	tombstone := header[12] == 1
	keyLen := binary.BigEndian.Uint32(header[13:17])

	buf := make([]byte, keyLen+4) // Key + ValLen (4B)
	if _, err := io.ReadFull(r.file, buf); err != nil {
		return nil, nil, 0, false, err
	}

	key := buf[:keyLen]
	valLen := binary.BigEndian.Uint32(buf[keyLen : keyLen+4])

	val := make([]byte, valLen)
	if _, err := io.ReadFull(r.file, val); err != nil {
		return nil, nil, 0, false, err
	}

	// Verify checksum integrity
	h := crc32.NewIEEE()
	h.Write(header[4:])
	h.Write(buf)
	h.Write(val)
	if h.Sum32() != checksum {
		return nil, nil, 0, false, ErrChecksumMismatch
	}

	return key, val, ts, tombstone, nil
}
```

## On-Disk Storage: SSTable Layout and Sparse Indexes

An SSTable is an immutable file that stores sorted key-value pairs. Since keys are sorted, we can optimize disk searches by grouping records into blocks (typically 4KB) and keeping a Sparse Index in memory. 

Rather than indexing the disk offset of every key, the Sparse Index only stores the starting key and file offset of each data block. To search for a key, we perform a binary search on the Sparse Index in memory to identify the specific block containing the key. Then, we execute a single random disk seek to load the 4KB block and scan it sequentially. This reduces memory usage by orders of magnitude compared to indexing every single record.

```go
// snippet-3
package storage

import (
	"bytes"
	"encoding/binary"
	"io"
)

// IndexEntry maps a block's starting key to its offset and size within the SSTable file.
type IndexEntry struct {
	Key    []byte
	Offset int64
	Length int64
}

// SparseIndex represents the in-memory array of index entries for fast block searches.
type SparseIndex []IndexEntry

// Search performs binary search to find the block that might contain the target key.
func (si SparseIndex) Search(key []byte) (int64, int64) {
	if len(si) == 0 {
		return -1, 0
	}

	low, high := 0, len(si)-1
	candidateIdx := -1

	for low <= high {
		mid := (low + high) / 2
		cmp := bytes.Compare(si[mid].Key, key)
		if cmp <= 0 {
			candidateIdx = mid
			low = mid + 1
		} else {
			high = mid - 1
		}
	}

	if candidateIdx == -1 {
		return si[0].Offset, si[0].Length
	}

	return si[candidateIdx].Offset, si[candidateIdx].Length
}

// Serialize writes the sparse index to the SSTable file during a flush.
func (si SparseIndex) Serialize(w io.Writer) error {
	if err := binary.Write(w, binary.BigEndian, uint32(len(si))); err != nil {
		return err
	}
	for _, entry := range si {
		if err := binary.Write(w, binary.BigEndian, uint32(len(entry.Key))); err != nil {
			return err
		}
		if _, err := w.Write(entry.Key); err != nil {
			return err
		}
		if err := binary.Write(w, binary.BigEndian, entry.Offset); err != nil {
			return err
		}
		if err := binary.Write(w, binary.BigEndian, entry.Length); err != nil {
			return err
		}
	}
	return nil
}
```

## Maximizing Read Performance with Bloom Filters

Even with sparse indexing, searching multiple levels of SSTables for a non-existent key requires a disk seek per SSTable. This read path penalty is called **Read Amplification**. 

To mitigate this, we prepend a Bloom Filter to each SSTable. A Bloom Filter is a space-efficient probabilistic data structure that can check whether an element is a member of a set. It never yields false negatives (if it says a key is not present, it is guaranteed not to be there), but it can yield false positives. Before querying the disk block, we query the Bloom Filter. If it returns false, we bypass the SSTable entirely.

```go
// snippet-4
package storage

import (
	"hash/fnv"
)

// BloomFilter provides a bit-array validation layer to bypass unnecessary disk reads.
type BloomFilter struct {
	bits []byte
	k    uint32 // Hash iterations
	m    uint32 // Bit length
}

func NewBloomFilter(numElements int, falsePositiveRate float64) *BloomFilter {
	// Standard formula approximations: m = - (n * ln(p)) / (ln(2)^2)
	// A bit budget of 10 bits per element yields a ~1% false positive rate with k = 7.
	m := uint32(numElements * 10)
	if m < 8 {
		m = 8
	}
	m = ((m + 7) / 8) * 8 // Align to byte boundary
	
	return &BloomFilter{
		bits: make([]byte, m/8),
		k:    7,
		m:    m,
	}
}

func (bf *BloomFilter) Add(key []byte) {
	h1, h2 := fnvDoubleHash(key)
	for i := uint32(0); i < bf.k; i++ {
		bitIdx := (h1 + i*h2) % bf.m
		bf.bits[bitIdx/8] |= (1 << (bitIdx % 8))
	}
}

func (bf *BloomFilter) Contains(key []byte) bool {
	h1, h2 := fnvDoubleHash(key)
	for i := uint32(0); i < bf.k; i++ {
		bitIdx := (h1 + i*h2) % bf.m
		if (bf.bits[bitIdx/8] & (1 << (bitIdx % 8))) == 0 {
			return false // Definitive negative
		}
	}
	return true // Probable positive
}

// fnvDoubleHash generates two 32-bit hashes for simulation of k distinct hash functions.
func fnvDoubleHash(data []byte) (uint32, uint32) {
	h := fnv.New32a()
	h.Write(data)
	h1 := h.Sum32()

	h2 := uint32(5381)
	for _, b := range data {
		h2 = ((h2 << 5) + h2) + uint32(b)
	}
	return h1, h2
}
```

## Serializing and Writing SSTables to Disk

When an active Memtable overflows, the flusher iterates through the sorted keys and serializes the dataset into a single SSTable file. To optimize disk usage, key and value lengths are written using variable-length integer encoding (varint). 

The output file consists of three segments:
1. **Data Blocks**: Contiguous lists of key-value pairs formatted into 4KB blocks.
2. **Index Block**: Located near the end of the file, containing serialized Sparse Index entries mapping block ranges.
3. **Bloom Filter Block**: Containing the filter's parameters and the bit array.
4. **Footer**: A fixed 24-byte tail containing the byte offsets of the Index and Bloom Filter blocks, terminated by an LSM magic number.

```go
// snippet-5
package storage

import (
	"encoding/binary"
	"io"
	"os"
)

type SSTableWriter struct {
	file         *os.File
	offset       int64
	blockSize    int64
	currBlockLen int64
	sparseIndex  SparseIndex
	bloomFilter  *BloomFilter
	lastKey      []byte
	entryCount   int
}

func NewSSTableWriter(path string, numElements int) (*SSTableWriter, error) {
	file, err := os.OpenFile(path, os.O_CREATE|os.O_WRONLY|os.O_TRUNC, 0644)
	if err != nil {
		return nil, err
	}
	return &SSTableWriter{
		file:        file,
		blockSize:   4096,
		bloomFilter: NewBloomFilter(numElements, 0.01),
	}, nil
}

func (w *SSTableWriter) Write(key, val []byte, ts int64, tombstone bool) error {
	// Initialize or boundary check for block partition
	if w.currBlockLen == 0 || w.currBlockLen >= w.blockSize {
		if w.currBlockLen >= w.blockSize {
			w.currBlockLen = 0
		}
		w.sparseIndex = append(w.sparseIndex, IndexEntry{
			Key:    append([]byte(nil), key...),
			Offset: w.offset,
		})
	}

	varHeader := make([]byte, 16)
	n1 := binary.PutUvarint(varHeader[0:], uint64(len(key)))
	n2 := binary.PutUvarint(varHeader[n1:], uint64(len(val)))
	
	recordHeaderSize := int64(n1 + n2 + 8 + 1)
	recordSize := recordHeaderSize + int64(len(key)) + int64(len(val))

	if _, err := w.file.Write(varHeader[:n1+n2]); err != nil {
		return err
	}

	tsBuf := make([]byte, 9)
	binary.BigEndian.PutUint64(tsBuf[0:8], uint64(ts))
	if tombstone {
		tsBuf[8] = 1
	} else {
		tsBuf[8] = 0
	}
	
	if _, err := w.file.Write(tsBuf); err != nil {
		return err
	}
	if _, err := w.file.Write(key); err != nil {
		return err
	}
	if _, err := w.file.Write(val); err != nil {
		return err
	}

	w.offset += recordSize
	w.currBlockLen += recordSize
	w.bloomFilter.Add(key)
	
	idx := len(w.sparseIndex) - 1
	w.sparseIndex[idx].Length = w.offset - w.sparseIndex[idx].Offset
	
	w.lastKey = append([]byte(nil), key...)
	w.entryCount++
	return nil
}

func (w *SSTableWriter) Close() error {
	// 1. Serialize Sparse Index
	indexOffset := w.offset
	if err := w.sparseIndex.Serialize(w.file); err != nil {
		return err
	}

	// Retrieve current position to set bloom offset
	bloomOffset, err := w.file.Seek(0, io.SeekCurrent)
	if err != nil {
		return err
	}

	// 2. Serialize Bloom Filter
	header := make([]byte, 8)
	binary.BigEndian.PutUint32(header[0:4], w.bloomFilter.k)
	binary.BigEndian.PutUint32(header[4:8], w.bloomFilter.m)
	if _, err := w.file.Write(header); err != nil {
		return err
	}
	if _, err := w.file.Write(w.bloomFilter.bits); err != nil {
		return err
	}

	// 3. Write Footer
	footer := make([]byte, 24)
	binary.BigEndian.PutUint64(footer[0:8], uint64(indexOffset))
	binary.BigEndian.PutUint64(footer[8:16], uint64(bloomOffset))
	binary.BigEndian.PutUint64(footer[16:24], 0x157743656e67696e) // LSM-Engine Magic
	
	if _, err := w.file.Write(footer); err != nil {
		return err
	}
	return w.file.Close()
}
```

## The Read Path: Cascading Query Resolution

Because the data is spread across the active Memtable, the immutable Memtable, and multiple SSTable files on disk, read queries must execute a cascading query resolution sequence. 

The engine checks storage components in reverse chronological order (newest to oldest):
1. Query the active Memtable.
2. Query the immutable Memtable (if present).
3. Query Level 0 SSTables (newest file first). Since L0 SSTables overlap, we must query all of them.
4. Query Level 1 and lower SSTables. Since higher levels do not overlap, we locate the single file whose key range covers the target key, and query only that file.

If a key is found, the engine checks if the record is a tombstone. If so, it returns a "not found" response, hiding the record as deleted.

```go
// snippet-6
package storage

import (
	"bytes"
	"io"
	"os"
)

type DB struct {
	activeMem   *Memtable
	immMem      *Memtable
	sstables    []*SSTableReader
}

type SSTableReader struct {
	file        *os.File
	bloomFilter *BloomFilter
	sparseIndex SparseIndex
}

func (db *DB) Get(key []byte) ([]byte, error) {
	// Step 1: Active Memtable
	if rec, ok := db.activeMem.Get(key); ok {
		if rec.tombstone {
			return nil, nil // Tombstone encountered: deleted
		}
		return rec.val, nil
	}

	// Step 2: Immutable Memtable
	if db.immMem != nil {
		if rec, ok := db.immMem.Get(key); ok {
			if rec.tombstone {
				return nil, nil
			}
			return rec.val, nil
		}
	}

	// Step 3: SSTables in reverse order (newest files checked first)
	for i := len(db.sstables) - 1; i >= 0; i-- {
		sstable := db.sstables[i]
		
		if !sstable.bloomFilter.Contains(key) {
			continue
		}

		offset, length := sstable.sparseIndex.Search(key)
		if offset < 0 {
			continue
		}

		val, tombstone, found, err := sstable.searchBlock(key, offset, length)
		if err != nil {
			return nil, err
		}
		if found {
			if tombstone {
				return nil, nil
			}
			return val, nil
		}
	}

	return nil, nil
}

func (r *SSTableReader) searchBlock(key []byte, offset, length int64) ([]byte, bool, bool, error) {
	blockBuf := make([]byte, length)
	if _, err := r.file.ReadAt(blockBuf, offset); err != nil && err != io.EOF {
		return nil, false, false, err
	}

	reader := bytes.NewReader(blockBuf)
	for {
		keyLen, err := binary.ReadUvarint(reader)
		if err == io.EOF {
			break
		}
		valLen, _ := binary.ReadUvarint(reader)
		
		tsBuf := make([]byte, 9)
		if _, err := io.ReadFull(reader, tsBuf); err != nil {
			return nil, false, false, err
		}
		tombstone := tsBuf[8] == 1

		k := make([]byte, keyLen)
		if _, err := io.ReadFull(reader, k); err != nil {
			return nil, false, false, err
		}

		v := make([]byte, valLen)
		if _, err := io.ReadFull(reader, v); err != nil {
			return nil, false, false, err
		}

		if bytes.Equal(k, key) {
			return v, tombstone, true, nil
		}
	}
	return nil, false, false, nil
}
```

## SSTable Merging: Multiway Merge-Sort and Compaction

As L0 SSTables accumulate, read performance degrades because the engine must search multiple files. Additionally, deleted keys (tombstones) and obsolete versions of keys still consume disk space. 

To clean this up, a background compaction worker performs a multiway merge-sort on selected SSTables. Since SSTables are sorted internally, we can merge $N$ files in $O(M \log N)$ time, where $M$ is the total number of records across all files, using a Min-Heap. The Min-Heap stores the head of each SSTable iterator. We pop the smallest key, write it to the new SSTable (discarding older versions of the same key based on timestamps), and advance the corresponding iterator.

```go
// snippet-7
package storage

import (
	"bytes"
	"container/heap"
)

// Iterator defines the interface for scanning key-value ranges across SSTables.
type Iterator interface {
	Next() bool
	Key() []byte
	Value() []byte
	Timestamp() int64
	IsTombstone() bool
	Error() error
}

type heapItem struct {
	iter Iterator
	idx  int
}

type MergeHeap []heapItem

func (h MergeHeap) Len() int           { return len(h) }
func (h MergeHeap) Less(i, j int) bool {
	cmp := bytes.Compare(h[i].iter.Key(), h[j].iter.Key())
	if cmp != 0 {
		return cmp < 0
	}
	// Tie breaker: Newer timestamp first to overwrite older records
	return h[i].iter.Timestamp() > h[j].iter.Timestamp()
}
func (h MergeHeap) Swap(i, j int)       { h[i], h[j] = h[j], h[i] }
func (h *MergeHeap) Push(x interface{}) { *h = append(*h, x.(heapItem)) }
func (h *MergeHeap) Pop() interface{} {
	old := *h
	n := len(old)
	item := old[n-1]
	*h = old[0 : n-1]
	return item
}

// MultiwayMergeIterator merges multiple iterators, discarding duplicate keys.
type MultiwayMergeIterator struct {
	iters    []Iterator
	mh       MergeHeap
	currKey  []byte
	currVal  []byte
	currTS   int64
	currTomb bool
}

func NewMultiwayMergeIterator(iters []Iterator) *MultiwayMergeIterator {
	m := &MultiwayMergeIterator{
		iters: iters,
		mh:    make(MergeHeap, 0, len(iters)),
	}
	for i, it := range iters {
		if it.Next() {
			heap.Push(&m.mh, heapItem{iter: it, idx: i})
		}
	}
	return m
}

func (m *MultiwayMergeIterator) Next() bool {
	if len(m.mh) == 0 {
		return false
	}

	top := heap.Pop(&m.mh).(heapItem)
	m.currKey = append([]byte(nil), top.iter.Key()...)
	m.currVal = append([]byte(nil), top.iter.Value()...)
	m.currTS = top.iter.Timestamp()
	m.currTomb = top.iter.IsTombstone()

	if top.iter.Next() {
		heap.Push(&m.mh, heapItem{iter: top.iter, idx: top.idx})
	}

	// Purge stale duplicates of the key from the heap
	for len(m.mh) > 0 {
		nextTop := m.mh[0]
		if bytes.Equal(nextTop.iter.Key(), m.currKey) {
			popped := heap.Pop(&m.mh).(heapItem)
			if popped.iter.Next() {
				heap.Push(&m.mh, heapItem{iter: popped.iter, idx: popped.idx})
			}
		} else {
			break
		}
	}

	return true
}

func (m *MultiwayMergeIterator) Key() []byte       { return m.currKey }
func (m *MultiwayMergeIterator) Value() []byte     { return m.currVal }
func (m *MultiwayMergeIterator) Timestamp() int64  { return m.currTS }
func (m *MultiwayMergeIterator) IsTombstone() bool { return m.currTomb }
```

## Production Failure Modes and Performance Tuning in Go

When running an LSM-Tree storage engine in production under high concurrency and sustained load, three primary failure modes occur:

### 1. Write Stalls
If write volume is high, the active Memtable will fill up faster than the background flusher can serialize the immutable Memtable to disk. When this happens, the engine runs out of memory or blocks incoming writes to prevent OOM errors. This is known as a **Write Stall**. 
* **Mitigation**: Introduce write rate-limiting at the API layer, increase the number of background flusher workers, or write-throttling inside the `Put` path when the count of pending L0 files exceeds a limit (e.g., more than 8 L0 files).

### 2. File Descriptor Exhaustion
Since every SSTable file represents a physical file on disk, having thousands of uncompacted SSTables will exhaust the operating system's file descriptor limits (typically `ulimit -n` defaults to 1024 on Linux). 
* **Mitigation**: Implement strict leveled compaction to keep the file count bound, and design an internal `TableCache` using an LRU cache scheme to pool open file descriptors across queries.

### 3. Garbage Collection Latency
Go's garbage collector traces heap pointers. Storing millions of small byte slices and structs in memory inside the SkipList can increase GC pause times to tens of milliseconds. 
* **Mitigation**: Store key-value pairs in a pre-allocated contiguous memory slab (an Arena allocator). Instead of allocating new byte slices for each key and value, copy the data to the Arena and reference offsets. This reduces the number of heap objects visible to the garbage collector to a single large slab.
