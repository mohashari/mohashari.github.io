---
layout: post
title: "Design Patterns dengan Go — Part 5: Singleton"
date: 2026-06-12
tags: [design-patterns, creational, golang, best-practices]
description: "Panduan mendalam implementasi design pattern Singleton di Go (Golang), menampilkan analogi pool koneksi database, keamanan thread (thread safety) dengan sync.Once, dan kode lengkap."
image: "https://picsum.photos/seed/5737/1080/720"
thumbnail: "https://picsum.photos/seed/5737/400/300"
---

Singleton adalah creational design pattern yang memungkinkan Anda memastikan bahwa sebuah struct hanya memiliki satu instance, sekaligus menyediakan titik akses global ke instance tersebut. Di Go, menerapkan Singleton memerlukan pertimbangan konkurensi yang cermat untuk mencegah terjadinya race condition selama inisialisasi.

## Penjelasan Konseptual & Analogi Dunia Nyata

Pola Singleton memecahkan dua masalah sekaligus, yang melanggar *Single Responsibility Principle* (Prinsip Tanggung Jawab Tunggal):
1. **Memastikan sebuah kelas hanya memiliki satu instance**: Mengapa ada orang yang ingin mengontrol berapa banyak instance yang dimiliki sebuah kelas? Alasan paling umum untuk hal ini adalah untuk mengontrol akses ke beberapa sumber daya bersama—misalnya, database atau file.
2. **Menyediakan titik akses global ke instance tersebut**: Ingat variabel global yang biasa kita gunakan untuk menyimpan beberapa objek penting? Meskipun sangat praktis, variabel tersebut juga sangat tidak aman karena kode apa pun berpotensi menimpa konten variabel tersebut dan merusak aplikasi.

Sama seperti pemerintah suatu negara, sebuah negara hanya dapat memiliki satu pemerintahan resmi. Terlepas dari identitas pribadi individu yang membentuk pemerintahan, gelar "Pemerintah Negara X" adalah titik akses global yang mengidentifikasi sekelompok orang yang memegang kekuasaan.

Dalam pengembangan perangkat lunak, Singleton biasanya digunakan untuk:
- Database connection pools (untuk menghindari kehabisan koneksi soket).
- Configuration managers (untuk membaca file konfigurasi sekali dan membagikannya secara global).
- Logging services (untuk menulis ke satu file dari banyak thread/goroutine secara bersamaan).

---

## Diagram Konseptual

Berikut adalah diagram kelas Mermaid yang menunjukkan struktur pola Singleton di Go:

```mermaid
classDiagram
    class DatabaseConnection {
        -connectionString string
        +Query(sql string) string
    }

    class SingletonAccessor {
        -instance *DatabaseConnection
        -once sync.Once
        +GetDatabaseConnection() *DatabaseConnection
    }

    SingletonAccessor --> DatabaseConnection : creates & manages
```

---

## Use Case / Skenario Masalah

Mengapa kita membutuhkan pola ini, dan mengapa Go berbeda?
Dalam bahasa seperti Java atau C++, Anda dapat membuat konstruktor kelas menjadi `private` untuk mencegah instansiasi langsung. Go tidak memiliki kelas, konstruktor, atau pengubah akses seperti `private` atau `public`. Sebaliknya, kita mengontrol akses menggunakan visibilitas tingkat paket (ditentukan oleh huruf besar/kecil di awal nama).

Selain itu, Go dirancang untuk konkurensi, menggunakan goroutine untuk mengeksekusi kode secara paralel. Jika dua goroutine memanggil fungsi pembuatan Anda pada milidetik yang sama, dan Anda belum menerapkan keamanan thread (thread safety), Anda akan membuat dua instance terpisah dari "Singleton" Anda, yang menggagalkan tujuan awal pola ini.

Untuk mengatasi hal ini di Go, kita menggunakan paket `sync`. Secara khusus, `sync.Once` menjamin bahwa blok kode dijalankan tepat sekali, bahkan jika dipanggil secara bersamaan dari ribuan goroutine.

---

## Contoh Kode Golang

Di bawah ini adalah program Go lengkap yang dapat dikompilasi, mendemonstrasikan pola Singleton menggunakan `sync.Once` untuk mengelola pool koneksi database bersama dengan cara yang aman dari thread (thread-safe).

```go
package main

import (
	"fmt"
	"sync"
	"time"
)

// databaseConnection mewakili objek singleton kita.
// Kita menggunakan nama dengan huruf kecil (privat untuk paket ini) untuk mencegah instansiasi langsung dari paket lain.
type databaseConnection struct {
	connectionString string
}

func (db *databaseConnection) Query(sql string) string {
	return fmt.Sprintf("Mengeksekusi query [%s] pada koneksi [%s]", sql, db.connectionString)
}

var (
	// instance adalah satu-satunya objek bersama.
	instance *databaseConnection
	// once memastikan bahwa inisialisasi hanya dijalankan sekali.
	once sync.Once
)

// GetDatabaseInstance menyediakan titik akses global ke instance singleton.
// Fungsi ini aman untuk thread dan menggunakan optimasi internal melalui sync.Once.
func GetDatabaseInstance() *databaseConnection {
	once.Do(func() {
		fmt.Println("Menginisialisasi pool koneksi database... (Ini seharusnya hanya terjadi SEKALI)")
		// Simulasikan jeda koneksi
		time.Sleep(100 * time.Millisecond)
		instance = &databaseConnection{
			connectionString: "postgres://user:password@localhost:5432/production_db",
		}
	})
	return instance
}

func main() {
	fmt.Println("Memulai Uji Coba Thread-Safety Singleton...")

	var wg sync.WaitGroup
	numGoroutines := 10

	// Jalankan beberapa goroutine yang mencoba mengakses singleton secara bersamaan
	for i := 1; i <= numGoroutines; i++ {
		wg.Add(1)
		go func(goroutineID int) {
			defer wg.Done()
			
			// Setiap goroutine memanggil GetDatabaseInstance
			db := GetDatabaseInstance()
			
			// Kita mencetak alamat memori untuk memverifikasi bahwa semua goroutine mendapatkan instance yang persis sama
			fmt.Printf("Goroutine %d: Pointer koneksi: %p\n", goroutineID, db)
		}(i)
	}

	wg.Wait()
	
	// Periksa kembali dengan memanggil sekali lagi dari goroutine utama
	finalInstance := GetDatabaseInstance()
	fmt.Printf("\nVerifikasi: Pointer pool koneksi akhir: %p\n", finalInstance)
	fmt.Println(finalInstance.Query("SELECT * FROM users;"))
}
```

---

## Ringkasan

### Keuntungan
- **Jaminan Instance Tunggal**: Anda dapat memastikan bahwa sebuah struct hanya memiliki satu instance.
- **Akses Global**: Anda mendapatkan titik akses global ke instance tersebut.
- **Inisialisasi Lambat (Lazy Initialization)**: Objek diinisialisasi hanya saat diminta pertama kali, menghemat sumber daya sistem jika tidak pernah digunakan.
- **Keamanan Thread Khas Go**: Dengan menggunakan `sync.Once`, Go menangani pengecekan lock secara efisien, menghindari penguncian global yang lambat pada panggilan berikutnya.

### Kekurangan
- **Melanggar Single Responsibility Principle**: Pola ini menyelesaikan dua masalah sekaligus.
- **Sulit di Unit Test**: Singleton memperkenalkan status global (global state), yang membuatnya lebih sulit untuk mengisolasi komponen selama pengujian. Anda tidak dapat dengan mudah melakukan mocking pada singleton.
- **Ketergantungan Tersembunyi (Hidden Dependencies)**: Pengguna singleton dapat mengaksesnya di mana saja, membuat dependensi menjadi tersembunyi dan kurang eksplisit dalam signature metode.

### Kapan Menggunakan
- Gunakan pola Singleton ketika sebuah struct di program Anda harus memiliki hanya satu instance yang tersedia untuk semua klien; misalnya, objek database bersama atau penyimpanan konfigurasi global.
- Gunakan pola Singleton ketika Anda memerlukan kontrol yang lebih ketat terhadap variabel global.
