# Redis PubSub

# Intro

Hey guys kali ini kita akan coba belajar salah satu feature dalam REDIS yaitu pubsub
pastikan teman teman sudah install redis di masing masing local env lalu masuk ke redis cli setelah masuk redis cli coba ketikan Help PUBLISH

![Help Publish](https://paper-attachments.dropboxusercontent.com/s_BB85CDB3BDC01F6DA6081FE52BD0DB6F3C8E16E182D6ED5721CB611424F65CD2_1669520882929_image.png)


berikut adalah detail publish command yang ada di redis 
lalu coba teman-teman cek juga command SUBSCRIBE seperti berikut 

![Help subscribe](https://paper-attachments.dropboxusercontent.com/s_BB85CDB3BDC01F6DA6081FE52BD0DB6F3C8E16E182D6ED5721CB611424F65CD2_1669520982109_image.png)


berikut detail subscribe command yang ada di redis 

terus bagaimana kita coba pubsub di redis nya …
kita mulai aja sekarang
yang perlu di persiapkan buka 2 redis-cli di local laptop 

![](https://paper-attachments.dropboxusercontent.com/s_BB85CDB3BDC01F6DA6081FE52BD0DB6F3C8E16E182D6ED5721CB611424F65CD2_1669528190886_image.png)


setelah buka 2 redis-cli masuk kan perintah subscribe terlebih dahulu dengan channel yang di inginkan seperti beriktu 

    SUBSCRIBE test
![](https://paper-attachments.dropboxusercontent.com/s_BB85CDB3BDC01F6DA6081FE52BD0DB6F3C8E16E182D6ED5721CB611424F65CD2_1669528304813_image.png)


setelah itu masuk ke redis-cli yang satu nya lalu ketikan perintah publish 

     PUBLISH test test-pubsub-bro

**test** di sini sebagai channel dan **test-pubsub-bro** sebagai message nya 

![](https://paper-attachments.dropboxusercontent.com/s_BB85CDB3BDC01F6DA6081FE52BD0DB6F3C8E16E182D6ED5721CB611424F65CD2_1669528371438_image.png)


setelah perintah publish di eksekusi maka di redis-cli yang menjalankan perintah subscribe akan muncul message nya seperti berikut 

![](https://paper-attachments.dropboxusercontent.com/s_BB85CDB3BDC01F6DA6081FE52BD0DB6F3C8E16E182D6ED5721CB611424F65CD2_1669528486453_image.png)


nah sampai sini sharing kita kali ini 
semoga bermanfaat 🫰🏻…

