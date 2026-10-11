# SSH Email Alerts

Linux sunucularda başarılı SSH girişlerini ve tekrarlanan başarısız giriş göstergelerini e-posta ile bildirir. Python standart kütüphanesi, systemd journal ve TLS destekli SMTP kullanır; ek Python paketi gerektirmez.

## Özellikler

- 15 saniyede bir SSH servislerinin son 50 kaydını kontrol eder.
- Başarılı girişleri kullanıcı, kaynak IP, yöntem ve seçilen saat dilimindeki zamanıyla bildirir.
- Aynı IP için 10 dakikada 5 başarısızlık göstergesinde uyarı gönderir; aynı IP için 10 dakika tekrar bildirim göndermez.
- SQLite üzerinde kalıcı takip noktası ve e-posta kuyruğu tutar. Gönderim hataları 60 saniye sonra tekrar denenir.
- İlk başlangıçta geçmiş kayıtlar için bildirim göndermez.
- Ham günlükleri, SMTP parolasını ve hata ayrıntılarını çıktıya yazmaz.

## Gereksinimler

Python 3.9+, systemd kullanan Linux, `ssh.service` veya `sshd.service` günlükleri ve SMTP hesabı. Servis sistem günlüklerini okuyabilmek için root olarak çalışır; systemd güvenlik kısıtlamaları uygulanır.

## Kurulum

Bu depo dizininde çalıştırın:

```bash
sudo install -d -m 755 /usr/local/lib/ssh-email-alerts
sudo install -m 644 ssh_email_alerts.py /usr/local/lib/ssh-email-alerts/ssh_email_alerts.py
sudo install -d -m 700 /etc/ssh-email-alerts
sudo install -m 600 .env.example /etc/ssh-email-alerts/config.env
sudoedit /etc/ssh-email-alerts/config.env
sudo install -m 644 systemd/ssh-email-alerts.service /etc/systemd/system/ssh-email-alerts.service
sudo systemctl daemon-reload
sudo python3 /usr/local/lib/ssh-email-alerts/ssh_email_alerts.py --test-email
sudo systemctl enable --now ssh-email-alerts.service
```

Örnek dosyadaki gönderen, alıcı ve SMTP parolasını değiştirin. Resend kullanıyorsanız `SMTP_PASSWORD` API anahtarını, `SMTP_USER=resend` kullanıcı adını ve doğrulanmış alan adınızdan bir göndereni kullanın. Port 465 doğrudan TLS, diğer portlar zorunlu STARTTLS kullanır. Sertifikalar doğrulanır.

`config.env` basit `KEY=value` biçimindedir; shell komutu, değişken genişletme ve çok satırlı değer desteklenmez. Gerçek ayarları GitHub'a yüklemeyin.

## Kişisel yapılandırma

Her kurulum kendi `config.env` dosyasını kullanır. SMTP sağlayıcısına özel hesap gerektirir; örnek dosyada gerçek kimlik bilgisi bulunmaz.

| Ayar | İşlev | Örnek |
|---|---|---|
| `SMTP_HOST` | SMTP sunucusu | `smtp.example.com` |
| `SMTP_PORT` | SMTP portu | `465` veya `587` |
| `SMTP_SECURITY` | TLS bağlantı şekli | `auto`, `ssl`, `starttls` |
| `SMTP_USER` | SMTP kullanıcı adı | `YOUR_SMTP_USER` |
| `SMTP_PASSWORD` | SMTP parolası / sağlayıcının uygulama parolası | `YOUR_SMTP_PASSWORD` |
| `EMAIL_FROM` | Gönderen adresi | `alerts@example.com` |
| `SENDER_NAME` | Gönderenin görünen adı | `SSH Alerts` |
| `EMAIL_TO` | Alıcı adresi | `you@example.com` |
| `LANGUAGE` | E-posta ve çalışma mesajlarının dili | `en` veya `tr` |
| `TIMEZONE` | IANA saat dilimi | `UTC`, `Europe/Istanbul` |

`SMTP_SECURITY=auto`, 465 portunda doğrudan TLS, diğer portlarda STARTTLS seçer. Özel port için `ssl` veya `starttls` açıkça seçilebilir. Şifresiz bağlantı desteklenmez. SMTP sunucusunun izin verdiği gönderen adresini kullanın.

Türkiye örneği: `LANGUAGE=tr` ve `TIMEZONE=Europe/Istanbul`. Dil seçimi saat dilimini değiştirmez; saat dilimi yaz/kış saati geçişlerini otomatik uygular. Geçersiz dil, saat dilimi veya port ayarı reddedilir. Linux dağıtımının saat dilimi verisi (genellikle `tzdata`) kurulu olmalıdır.

Ayarlar kontrollerde yeniden okunur; kuyrukta bulunan e-postalar oluşturuldukları dil ve saat dilimini korur. Çalışma mesajları seçilen dildedir; CLI yardım metni İngilizcedir.

## Kontrol ve seçenekler

```bash
sudo systemctl is-active ssh-email-alerts.service
sudo journalctl -u ssh-email-alerts.service -n 50 --no-pager
python3 ssh_email_alerts.py --help
python3 -m unittest discover -s tests -v
```

`--config PATH` farklı ayar dosyası seçer; varsayılan `/etc/ssh-email-alerts/config.env` yoludur. `SSH_ALERT_CONFIG` ortam değişkeni de desteklenir. `--state-dir PATH` kalıcı veri dizinini seçer. `--once` tek kontrol yapar; `--test-email` test mesajı gönderir.

## Sınırlar

Bu araç IP engellemez. Bir bağlantı birden fazla başarısızlık göstergesi oluşturabilir; eşik benzersiz bağlantı sayısı değildir. Her kontrolde yalnızca en yeni 50 kayıt okunur: yüksek trafik veya uzun kesintilerde aradaki olaylar atlanabilir ve yoğunluk bildirimi oluşturulur. Journal silinir veya takip noktası geçersiz olursa servis kontrol hatası bildirir; otomatik takip noktası sıfırlaması yoktur.

SMTP kabulü gelen kutusuna teslim garantisi değildir. SMTP kabulünden sonra süreç kesilirse aynı mesaj tekrar gönderilebilir. Uzun SMTP kesintilerinde kuyruk büyüyebilir. Root erişimini ele geçiren saldırgan günlükleri ve servisi değiştirebilir.

## Lisans

Henüz lisans seçilmedi. Açık kaynak kullanım ve dağıtım izni için yayımlamadan önce lisans ekleyin.
