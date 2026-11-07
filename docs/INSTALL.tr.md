# HealtOS kurulumu

*English: [INSTALL.md](INSTALL.md).*

Bu rehber adım adım kurulum aracını (Linux, Raspberry Pi, NAS, VPS ve macOS için `install.sh`;
Windows için `install.ps1`), desteklenen her platformu, veri kaynaklarını, yedekleri,
güncellemeyi ve sorun gidermeyi anlatır. Kurulum aracı yalnızca [README](../README.md#manual-setup)
ve [dağıtım rehberindeki](DEPLOYMENT.md) elle kurulum adımlarını otomatikleştirir; aynı adımları
her zaman elle de yapabilirsiniz.

**Varsayılan hedef bir Linux sunucudur.** HealtOS sürekli açık duran bir makinede çalışmak için
tasarlandı: Linux sunucu ya da sanal makine, Raspberry Pi veya NAS. Masaüstü ya da dizüstü
bilgisayar da olur, ama arşiv yalnızca makine uyanıkken güncel kalır.

- [Hızlı başlangıç](#hızlı-başlangıç) · [Yapay zekâ ajanıyla kurulum](#yapay-zekâ-ajanıyla-kurulum)
- [Kurulum aracının soruları](#kurulum-aracının-soruları)
- [Komut satırı seçenekleri](#komut-satırı-seçenekleri)
- [Kurulum aracının değiştirdikleri ve asla yapmadıkları](#kurulum-aracının-değiştirdikleri-ve-asla-yapmadıkları)
- [Platformlar](#platformlar): [Linux](#linux-sunucu-ya-da-sanal-makine-varsayılan) ·
  [Raspberry Pi](#raspberry-pi-ve-diğer-arm64-kartlar) · [NAS](#nas-synology-qnap-unraid-truenas-scale) ·
  [macOS](#macos) · [Windows](#windows-10-ve-11) · [Bulut VPS](#bulut-vps)
- [Veri kaynakları](#veri-kaynakları)
- [Yedekler](#yedekler)
- [Güncelleme](#güncelleme)
- [Veri kaybetmeden durdurma ve kaldırma](#veri-kaybetmeden-durdurma-ve-kaldırma)
- [Sorun giderme](#sorun-giderme)

## Hızlı başlangıç

**Linux, Raspberry Pi, VPS, macOS** (bash; kendi kullanıcınızla ya da root olarak çalıştırın). Kurulum aracı
kendisi asla `sudo` çağırmaz: root iken sistem birimleri, normal kullanıcıyken kullanıcı birimleri
kurar; `sudo` gerektiren komutlar yazdırılır, onları siz çalıştırırsınız. Türkçe sorular için sonuna `-s -- --lang tr` ekleyin:

```sh
curl -fsSL https://raw.githubusercontent.com/M3rgen/HealtOS/main/install.sh | bash
curl -fsSL https://raw.githubusercontent.com/M3rgen/HealtOS/main/install.sh | bash -s -- --lang tr
```

**Windows 10/11** (PowerShell, kendi kullanıcınızla):

```powershell
irm https://raw.githubusercontent.com/M3rgen/HealtOS/main/install.ps1 | iex
```

**Sabit bir sürüm** (güncel `main` yerine, burada v0.2.2):

```sh
curl -fsSL https://raw.githubusercontent.com/M3rgen/HealtOS/v0.2.2/install.sh | bash -s -- --ref v0.2.2
```

```powershell
& ([scriptblock]::Create((irm https://raw.githubusercontent.com/M3rgen/HealtOS/v0.2.2/install.ps1))) -Ref v0.2.2
```

**Betiği çalıştırmadan önce okumak mı istersiniz?** Önce depoyu klonlayın, `install.sh` ya da
`install.ps1` dosyasını okuyun, sonra klonun içinden çalıştırın; o zaman aynı klonu kullanır ve
yeniden klonlamaz:

```sh
git clone https://github.com/M3rgen/HealtOS.git
cd HealtOS
less install.sh
./install.sh --lang tr            # ya da önce: ./install.sh --lang tr --dry-run
```

```powershell
git clone https://github.com/M3rgen/HealtOS.git
cd HealtOS
notepad .\install.ps1
powershell -ExecutionPolicy Bypass -File .\install.ps1 -Lang tr     # ya da -DryRun ekleyin
```

### Yapay zekâ ajanıyla kurulum

Kabuk erişimi olan bir kodlama ajanı (Claude Code, OpenAI Codex CLI ya da benzeri), kurulumu
[AI_INSTALL.md](AI_INSTALL.md) adımlarını izleyerek sizin yerinize yapabilir. Ona şu istemi verin:

```text
Bu bilgisayara HealtOS'u kur: https://raw.githubusercontent.com/M3rgen/HealtOS/main/docs/AI_INSTALL.md adımlarını izle; yönetici izni ya da hesap bilgisi gereken her adımdan önce bana sor ve hiçbir veriyi silme.
```

Terminalde doğrudan çalıştırmak için:

```sh
claude "Bu bilgisayara HealtOS'u kur: https://raw.githubusercontent.com/M3rgen/HealtOS/main/docs/AI_INSTALL.md adımlarını izle; yönetici izni ya da hesap bilgisi gereken her adımdan önce bana sor ve hiçbir veriyi silme."
codex "Bu bilgisayara HealtOS'u kur: https://raw.githubusercontent.com/M3rgen/HealtOS/main/docs/AI_INSTALL.md adımlarını izle; yönetici izni ya da hesap bilgisi gereken her adımdan önce bana sor ve hiçbir veriyi silme."
```

İsterseniz istemi Claude Code'a, Codex'e ya da komut çalıştırabilen başka bir ajana yapıştırın.
Ajan size kurulum aracının sorduğu soruların aynısını sorar, önce bir `--dry-run` gösterir, sonra
kurulum aracını yanıtlarınızla soru sormadan çalıştırır.

### Bir çalıştırma ne yapar

Sırasıyla:

1. Makineyi denetler: Git, Compose v2 ile Docker ve çalışan bir Docker motoru. Eksik bir şey varsa
   işletim sisteminize göre nasıl kurulacağını söyler ve durur (çıkış kodu 2); kendiliğinden
   sistem paketi kurmaz.
2. Bir HealtOS klonunun içinde çalışmıyorsa `https://github.com/M3rgen/HealtOS.git` deposunu
   `--ref` (varsayılan `main`) ile kurulum dizinine klonlar.
3. Aşağıdaki soruları sorar (ya da yanıtları seçeneklerden alır).
4. `.env` dosyasını yazar: yalnızca eksik değerler için yeni gizli anahtar üretir; var olan bir
   `.env` önce `.env.bak-<UTC zaman damgası>` olarak kopyalanır; yeniden çalıştırma yalnızca
   [Kurulum aracının değiştirdikleri](#kurulum-aracının-değiştirdikleri-ve-asla-yapmadıkları)
   bölümündeki birkaç değeri değiştirebilir. Gizli anahtarlar asla değiştirilmez.
5. İsterseniz yedek anahtarını oluşturur; günlük yedeği, yeniden başlatmadan sonra otomatik
   açılışı ve Umay köprüsünü makinenin kendi zamanlayıcısıyla (systemd, launchd ya da Görev
   Zamanlayıcı) kurar.
6. `--no-start` verilmediyse ve tam kipte eksik değer yoksa yığını derleyip başlatır
   (`docker compose up -d --build`),
   `/health/live` yanıt verene kadar bekler, sonra pano adresini, erişim anahtarını (bir kez) ve
   sonraki adımları yazar.

Ardından makinede **<http://localhost:8000>** adresini açın (sunucudaysa SSH tüneli üzerinden,
[aşağıya](#panoyu-başka-bir-bilgisayardan-açmak) bakın) ve anahtarı girin. İlk derleme
bağımlılıkları indirir ve birkaç dakika sürer; Raspberry Pi'de çok daha uzun.

## Kurulum aracının soruları

Her sorunun önceden seçili bir yanıtı vardır; Enter onu kabul eder. Sorular İngilizce ya da
Türkçedir.

1. **Dil.** English (önceden seçili) ya da Türkçe.
2. **Platform.** Altı seçeneğin hepsi her zaman listelenir. Linux makinede varsayılan Linux'tur; macOS
   ya da Windows üzerinde kurulum aracı neyi algıladığını söyler ve o makinenin kendi platformunu
   önceden seçer, çünkü Linux kurulumu orada yapılamaz.

   | # | Platform | Not |
   | --- | --- | --- |
   | 1 | Linux sunucu / sanal makine (Debian, Ubuntu, Fedora…; Proxmox VM/LXC) | varsayılan, önerilen |
   | 2 | Raspberry Pi / ARM64 kart | 64 bit işletim sistemi şart; 4 GB ya da daha fazla RAM önerilir; ilk derleme yavaştır |
   | 3 | NAS (Synology Container Manager, QNAP Container Station, Unraid, TrueNAS SCALE) | `.env` dosyasını hazırlar; gerisi [NAS bölümünde](#nas-synology-qnap-unraid-truenas-scale) |
   | 4 | macOS (Apple Silicon ya da Intel) | Docker Desktop, OrbStack ya da Colima |
   | 5 | Windows 10/11 | WSL 2 altyapılı Docker Desktop |
   | 6 | Bulut VPS | Linux ile aynı, artı SSH tüneli notu |

3. **Kip.** `trial` (deneme) ya da `full` (tam); bkz. [Deneme ve tam kip](#deneme-ve-tam-kip).
   İlk çalıştırmada `trial` önceden seçilidir. Yeniden çalıştırmada önceden seçilen yanıt (ve
   `--yes` ile varsayılan) var olan `.env` dosyasından gelir: `docker-compose.dev.yml` içermeyen bir
   `COMPOSE_FILE` `full`, içeren `trial` demektir; `.env` ya da `COMPOSE_FILE` yoksa `trial`.
   `--mode` her zaman önceliklidir.
4. **Veri kaynakları.** Google Health API v4, Garmin Connect Personal API ve Apple Sağlık'ın
   herhangi bir birleşimi ya da hiçbiri; her biri [Veri kaynakları](#veri-kaynakları) altında
   anlatılır. Google için OAuth istemci kimliğini ve gizli anahtarını (Enter sonraya bırakır),
   tam kipte de herkese açık webhook ana makine adını sorar; Garmin için anahtar dosyasının yolunu
   (dosya henüz yoksa anahtarı yapıştırabilirsiniz, oraya kaydedilir); Apple için Health Auto
   Export alıcısının açılıp açılmayacağını (varsayılan hayır) ve HTTPS adresini.
5. **Kurulum dizini.** Yalnızca klonlaması gerekiyorsa: Linux'ta root olarak `/opt/healthos`,
   aksi halde `~/healtos` (Windows'ta `%USERPROFILE%\healtos`). Dizin boş olmalı ya da hiç
   olmamalı; kurulum aracı var olan dosyaların üzerine asla klonlamaz.
6. **Günlük şifreli yedek.** Tam kipte varsayılan evet, deneme kipinde hayır. Yedek anahtarı yoksa
   oluşturmayı önerir; bkz. [Yedekler](#yedekler).
7. **Yeniden başlatmadan sonra açılış.** Varsayılan evet.
8. **Umay köprüsü hizmet olarak.** Varsayılan hayır; bkz. [Umay](../README.md#optional-umay).

Sonunda planı (platform, kip, kaynaklar, dizin, makine işleri, `COMPOSE_FILE`) gösterir ve onay
ister; hayır derseniz hiçbir şey değişmez.

### Deneme ve tam kip

| | `trial` (deneme) | `full` (tam) |
| --- | --- | --- |
| Amaç | İlk yerel bakış, geliştirme, boş pano ve elle girişler | Gözetimsiz çalışma |
| Compose dosyaları | `docker-compose.yml` + `docker-compose.dev.yml` | `docker-compose.yml` |
| `APP_ENV` (Compose dosyaları belirler, `.env` değil) | `development` (üretim gizli anahtar denetimi atlanır) | `production` |
| Google | isteğe bağlı | `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `GOOGLE_TOKEN_ENCRYPTION_KEY`, `GOOGLE_HEALTH_WEBHOOK_ENDPOINT_URI` (HTTPS) ve `GOOGLE_HEALTH_WEBHOOK_AUTHORIZATION` |
| PostgreSQL portu | `127.0.0.1:5432` üzerinde açık | açık değil |
| `/docs` API belgeleri | var | yok |

İki kipte de Garmin ya da Apple alıcısı seçilirse `docker-compose.garmin.yml` ve
`docker-compose.apple.yml` eklenir.

**Yeniden çalıştırma kipi korur.** `--mode` verilmezse var olan `.env` okunur:
`docker-compose.dev.yml` içermeyen bir `COMPOSE_FILE` `full`, içeren `trial` demektir; `.env` ya da
`COMPOSE_FILE` yoksa `trial`. Sihirbaz da aynı yanıtı önceden seçer. Değiştirmek için `--mode` verin.

Tam kipte uygulama, üretim değerlerinin hepsi girilene kadar başlamayı reddeder. Kurulum aracı
kapsayıcının sonradan çökmesini beklemek yerine eksik olanları listeler ve başlatmadan
yapılandırır; değerleri `.env` dosyasına yazıp kurulum aracını yeniden çalıştırın (ya da
`docker compose up -d --build`).

**Sonradan denemeden tama geçmek.** Kurulum aracını `--mode full` ile yeniden çalıştırın.
`COMPOSE_FILE` satırını yeni seçime göre yeniden yazar (geliştirme eki çıkar; oraya kendiniz
eklediğiniz başka dosyalar kalır), hiçbir gizli anahtarı değiştirmez ve hâlâ eksik olan üretim
değerlerini listeler. Verileriniz aynı veritabanı biriminde kalır.

## Komut satırı seçenekleri

`install.sh` ve `install.ps1` aynı seçenekleri alır. Seçenek verilmezse kurulum aracı sorar;
`--yes` / `-NonInteractive` ile hiç sormaz, seçenekleri ve varsayılanları kullanır.

| `install.sh` | `install.ps1` | Anlamı |
| --- | --- | --- |
| `--lang en\|tr` | `-Lang en\|tr` | Soruların ve iletilerin dili. Etkileşimli varsayılan: sorar, English önceden seçili. |
| `--platform linux\|raspberrypi\|nas\|macos\|windows\|vps` | `-Platform …` | Hedef platform (yukarıdaki tablo). |
| `--mode trial\|full` | `-Mode trial\|full` | İlk çalıştırmada varsayılan `trial`; yeniden çalıştırmada var olan `.env` dosyasının kipi (bkz. [Deneme ve tam kip](#deneme-ve-tam-kip)). Açıkça verilen değer her zaman önceliklidir. |
| `--sources google,garmin,apple` ya da `none` | `-Sources …` | Virgülle ayrılmış veri kaynakları. |
| `--dir YOL` | `-Dir YOL` | Kurulum dizini. Varsayılan: bir klonun içinde çalışıyorsa o klon; değilse Linux'ta `/opt/healthos` (root) ya da `~/healtos`, macOS'ta `~/healtos`, Windows'ta `%USERPROFILE%\healtos`. |
| `--ref REF` | `-Ref REF` | Klonlanacak Git dalı ya da etiketi (varsayılan `main`). |
| `--garmin-key-file YOL` | `-GarminKeyFile YOL` | Makinedeki Garmin Personal API anahtar dosyası. Açıkça verilen yol yoksa hata (çıkış kodu 2). Seçenek verilmezse eksik varsayılan anahtar dosyası Garmin'i bir notla kapalı bırakır, çıkış kodu 0. |
| `--google-client-id ID` | `-GoogleClientId ID` | Google OAuth istemci kimliği. |
| `--google-client-secret S` | `-GoogleClientSecret S` | Google OAuth istemci gizli anahtarı. |
| `--apple-receiver yes\|no` | `-AppleReceiver yes\|no` | Health Auto Export alıcısını açar (varsayılan no). |
| `--backups yes\|no` | `-Backups yes\|no` | Günlük şifreli yedeği zamanlar (tam kipte varsayılan yes, deneme kipinde no). |
| `--autostart yes\|no` | `-Autostart yes\|no` | Yeniden başlatmadan sonra HealtOS'u geri getirir (varsayılan yes). |
| `--umay yes\|no` | `-Umay yes\|no` | Umay köprüsünü hizmet olarak kurar (varsayılan no). |
| `--yes`, `--non-interactive` | `-NonInteractive` | Hiç sormaz; seçenekleri ve varsayılanları kullanır. |
| `--dry-run` | `-DryRun` | Her eylemi yapmadan yazar: klonlama, Docker ve hizmet yok. Yazacağı `.env` dosyası `--env-out` yoluna gider (varsayılan `<dizin>/.env.dry-run`); gerçek bir dosyanın üzerine asla yazmaz. |
| `--env-out YOL` | `-EnvOut YOL` | `--dry-run` kipinin yazacağı `.env` dosyasının yeri. Yalnızca `--dry-run` ile geçerlidir (aksi halde çıkış kodu 2); klonun `.env` dosyasını, her `.env.bak-*` dosyasını ve önceki bir önizleme olmayan var olan her dosyayı reddeder. |
| `--no-start` | `-NoStart` | Yalnızca yapılandırır; yığını derlemez ve başlatmaz. |
| `--help` | `-Help` | İngilizce ve Türkçe kullanım bilgisi. |

Çıkış kodları: `0` başarılı, `1` bir adım başarısız oldu, `2` hatalı seçenek ya da desteklenmeyen
ortam (örneğin Git ya da Docker yok, 32 bit işletim sistemi).

`--google-client-secret` gibi değerler kabuk geçmişinize yazılabilir; bunları vermeyin, kurulum
aracı sorsun ya da sonradan `.env` dosyasına yazın.

Örnekler:

```sh
# Linux sunucu, soru sormadan, Google ve Garmin ile tam kip:
./install.sh --yes --lang tr --platform linux --mode full --sources google,garmin \
  --garmin-key-file ~/.config/garmin/connect-personal-api-key

# Bir NAS'ta ne olacağını hiçbir şeye dokunmadan görmek:
./install.sh --lang tr --platform nas --dry-run

# Yalnızca yapılandır, sonra kendin başlat:
./install.sh --lang tr --mode trial --sources none --no-start
```

```powershell
.\install.ps1 -NonInteractive -Lang tr -Platform windows -Mode trial -Sources apple
```

## Kurulum aracının değiştirdikleri ve asla yapmadıkları

HealtOS her kaydı ve her yedeği saklar; kurulum aracı da aynı kurala uyar.

- **Her değişiklikten önce `.env` kopyalanır.** Dosya varsa önce `.env.bak-<UTC zaman damgası>`
  olarak kopyalanır (eski `.bak` dosyaları kalır). Sonra kurulum aracı eksik ya da boş anahtarları
  doldurur ve var olan değerlerden yalnızca şunları değiştirebilir: `COMPOSE_FILE` ve
  `COMPOSE_PATH_SEPARATOR` (güncel kip ve kaynaklarınıza uyar), seçtiğiniz kaynağın
  `GARMIN_ENABLED` / `APPLE_RECEIVER_ENABLED` değeri `false` ise `true` olur ve
  `--garmin-key-file` / `-GarminKeyFile` açıkça verilirse Garmin anahtar dosyası yolu. Her değişiklik
  bildirilir. Bayrak yalnızca değeri boşsa ya da `false`, `0`, `no`, `off` (büyük/küçük harf fark
  etmez) ise `true` yapılır. Bir kaynağı asla kapatmaz (`…_ENABLED=false` değerini kendiniz yazın); var olan diğer
  değerler olduğu gibi kalır. `APP_ENV` yazılmaz, onu Compose dosyaları belirler. Var olan bir gizli
  anahtar asla değiştirilmez: yeni bir
  `POSTGRES_PASSWORD` sizi var olan veritabanının dışında bırakırdı.
- **Gizli anahtarlar** `openssl` ya da `/dev/urandom` (`install.sh`) veya .NET'in rastgele sayı
  üreteciyle (`install.ps1`) üretilir; makinede Python gerekmez. `POSTGRES_PASSWORD` 64
  onaltılık karakter, `APP_UI_TOKEN` en az 43 URL-güvenli karakter, `GOOGLE_TOKEN_ENCRYPTION_KEY`
  bir Fernet anahtarıdır.
- **Compose dosyaları `.env` içinde seçilir:** `COMPOSE_FILE` ve `COMPOSE_PATH_SEPARATOR`
  (Unix'te `:`, Windows'ta `;`). Böylece sonradan düz `docker compose ps`, `logs`, `up -d` ve
  `exec` komutları `-f` vermeden aynı dosyaları kullanır. Garmin'li bir deneme kurulumu örneği:
  `COMPOSE_FILE=docker-compose.yml:docker-compose.dev.yml:docker-compose.garmin.yml`.
  Kurulum aracı yalnızca `docker-compose.yml`, `docker-compose.dev.yml`,
  `docker-compose.garmin.yml`, `docker-compose.apple.yml` ve `docker-compose.windows.yml`
  dosyalarını yönetir; `COMPOSE_FILE` içindeki diğer her girdi (kendi dosyalarınız) özgün sırasıyla
  korunur. `COMPOSE_FILE` tanımlı değilse ve `docker-compose.override.yml` varsa o da eklenir,
  çünkü Compose onu zaten örtük olarak yüklerdi.
- **Asla:** `docker compose down -v`, birim (volume) silme, yedek silme, herhangi bir dosyayı
  silme. Kurulum aracını yeniden çalıştırmak güvenlidir.
- Erişim anahtarı sonda bir kez yazdırılır. `.env` içinde `APP_UI_TOKEN` olarak kalır.

## Platformlar

Bütün platformlar aynı `docker-compose.yml` dosyasını, aynı imajları (amd64 ve arm64) ve aynı
`.env` dosyasını kullanır. Yalnızca makine işleri farklıdır:

| Platform | Yedek zamanlaması | Yeniden başlatmadan sonra açılış | Umay köprüsü hizmeti |
| --- | --- | --- | --- |
| Linux / Raspberry Pi / VPS | systemd zamanlayıcısı | Docker hizmeti etkin; kapsayıcılar `restart: unless-stopped` | systemd birimi |
| NAS | NAS'ın kendi görev zamanlayıcısı (elle) | NAS'ın Docker uygulaması; kapsayıcılar `restart: unless-stopped` | desteklenmiyor |
| macOS | launchd ajanı | oturum açılınca Docker Desktop / OrbStack / Colima | launchd ajanı |
| Windows | Görev Zamanlayıcı görevi | oturum açılınca Docker Desktop | zamanlanmış görev |

### Linux sunucu ya da sanal makine (varsayılan)

**Gerekenler:** 64 bit Debian, Ubuntu, Fedora ya da benzeri bir dağıtım; Compose eklentisiyle
Docker Engine ([Docker'ın kurulum rehberi](https://docs.docker.com/engine/install/)); Git; yedekler
için `age` ve Python 3.11+. Yığın için 2 GB RAM yeter; disk arşivinizle birlikte büyür. Proxmox:
bir sanal makine ya da Docker için iç içe sanallaştırması (nesting) açık bir LXC kapsayıcısı.

```sh
sudo apt install git age python3      # Fedora: sudo dnf install git age python3
sudo systemctl enable --now docker
docker compose version
curl -fsSL https://raw.githubusercontent.com/M3rgen/HealtOS/main/install.sh | bash -s -- --lang tr
```

**root ya da kendi kullanıcınız.**

- **root olarak** (tek satırlık komutta `bash` yerine `sudo bash`, ya da `sudo bash install.sh`):
  kurulum `/opt/healthos` dizinine, birimler `/etc/systemd/system/` altına, yedek anahtarı
  `/etc/healthos/backup.key` yoluna gider. Bu, [dağıtım rehberiyle](DEPLOYMENT.md) aynıdır.
- **Kendi kullanıcınızla** (kullanıcı `docker` çalıştırabilmeli, yani `docker` grubunda
  olmalı): kurulum `~/healtos`, yedek anahtarı `~/.config/healtos/backup.key`. Etkileşimli
  çalıştırmada kurulum aracı, sistem birimleri için `sudo` ile yeniden çalıştırabilmeniz için
  burada durmayı da önerir. Birimler systemd
  **kullanıcı** birimleri olarak kurulur (`~/.config/systemd/user/`, `systemctl --user` ile
  yönetilir) ve açık bir oturum olmadan da çalışsınlar diye kalıcılık (lingering) açılır
  (`loginctl enable-linger`).

**systemd birimleri** ([`deploy/systemd/`](../deploy/systemd/) içinden, kurulum yolu
doldurularak; kullanıcı birimlerinde `/etc/healthos` yerine `~/.config/healtos`):

| Birim | Ne yapar |
| --- | --- |
| `healthos-backup.timer` / `healthos-backup.service` | `scripts/backup.sh` betiğini her gün 03:30'da çalıştırır; makine kapalıyken kaçan çalıştırmayı sonra yapar. |
| `healthos-backup-failure.service` | Başarısız bir yedeği `crit` düzeyinde günlüğe yazar (`journalctl -p crit -t healthos-backup`). |
| `healthos-bridge.service` | İsteğe bağlı Umay köprüsü; yalnızca `--umay yes` ile. |

```sh
systemctl list-timers healthos-backup.timer
sudo systemctl start healthos-backup.service && journalctl -u healthos-backup.service -n 20
systemctl status healthos-bridge.service            # kuruluysa
# Kendi kullanıcınızla kurulduysa: aynı komutlar --user ile, sudo olmadan, örneğin
systemctl --user list-timers healthos-backup.timer
journalctl --user -u healthos-backup.service -n 20
```

Linux'ta otomatik açılış için ayrı bir birim gerekmez: kurulum aracı Docker hizmetini etkinleştirir
(root olarak; değilse `sudo systemctl enable docker` çalıştırmanızı söyler), kapsayıcılar
`restart: unless-stopped` ile geri gelir.

**systemd yoksa** (örneğin Alpine ya da systemd'siz bir LXC kapsayıcısı) kurulum aracı birim
kurmaz; yedek anahtarını oluşturur, günlük yedek için bir `crontab` satırı ve Docker'ı açılışta
başlatmak için bir ipucu yazar.

Üretim sunucusunun geri kalanı (webhook tüneli, köprü için güvenlik duvarı, son kontrol listesi)
için [dağıtım rehberine](DEPLOYMENT.md) 5. adımdan devam edin.

#### Panoyu başka bir bilgisayardan açmak

Uygulama sunucuda yalnızca `127.0.0.1` adresini dinler; bu değişmez. Kendi bilgisayarınızdan:

```sh
ssh -N -L 8000:127.0.0.1:8000 <kullanıcı>@<sunucu>
```

Sonra <http://localhost:8000> (ya da <http://healthos.localhost:8000>, bkz.
[dağıtım 5. adım](DEPLOYMENT.md#5-open-the-dashboard-through-ssh)) adresini açın. Windows 10/11'de
aynı `ssh` komutu hazır gelir.

### Raspberry Pi ve diğer ARM64 kartlar

- **64 bit işletim sistemi şart:** Raspberry Pi OS (64-bit) ya da ARM64 için Ubuntu Server.
  HealtOS'un kullandığı her imaj amd64 ve arm64 için yapılmıştır; 32 bit ARM (armv7)
  desteklenmez, çünkü Node.js ve `uv` armv7 sürümü yayımlamıyor. Kurulum aracı 32 bit sistemi
  reddeder (çıkış kodu 2); `uname -m` ile denetleyin, `aarch64` yazmalı. Pi 3 ve sonrası 64 bit
  işletim sistemi çalıştırabilir.
- **RAM:** 4 GB ya da daha fazlası önerilir (Pi 4 ya da Pi 5). 2 GB yığını çalıştırabilir ama ilk
  derlemede büyük olasılıkla bellek yetmez; denerseniz takas alanı (swap) ekleyin.
- **İlk derleme yavaştır:** önyüz ve Python bağımlılıkları kartın üzerinde derlenir; Pi 4'te
  15–30 dakika bekleyin. Sonraki güncellemeler önbellekteki katmanları kullanır.
- **Depolama:** PostgreSQL sürekli yazar; bu SD kartları yıpratır ve onlarda yavaştır. Docker'ın
  veri dizinini (ve onunla `postgres_data` birimini) bir USB 3 SSD'ye koyun ya da Pi'yi SSD'den
  başlatın. Yedekleri veritabanından farklı bir diskte tutun.
- Docker'ı [Docker'ın Debian talimatlarıyla](https://docs.docker.com/engine/install/debian/)
  (ya da `curl -fsSL https://get.docker.com | sh`) kurun, sonra Linux tek satırlık komutunu
  çalıştırın. Kurulum aracı Linux'u önceden seçer; *Raspberry Pi* seçin ya da
  `--platform raspberrypi` verin.

Geri kalan her şey (systemd birimleri, SSH tüneli, yedekler)
[Linux](#linux-sunucu-ya-da-sanal-makine-varsayılan) ile aynıdır.

### NAS (Synology, QNAP, Unraid, TrueNAS SCALE)

NAS aynı Compose dosyalarını ve aynı `.env` dosyasını çalıştırır; yalnızca başlatma yolu farklıdır.
Kurulum aracının NAS seçeneği klonu ve `.env` dosyasını hazırlar ve kalan adımları yazar; NAS'a
hizmet kurmaz.

**Gerekenler:** Docker/Compose uygulaması ve SSH erişimi olan x86-64 ya da ARM64 bir NAS, imajı
derlemek için yeterli RAM (2 GB boş). Projeyi USB diske değil, normal bir veri biriminde tutun.

**1. adım: klonu ve `.env` dosyasını NAS'a koyun.** Birini seçin:

- *NAS'ta SSH ve Git varsa* (SSH açıldıktan sonra çoğu modelde): SSH ile bağlanın, paylaşılan bir
  klasöre geçin (örneğin Synology'de `/volume1/docker`, QNAP'ta `/share/Container`, Unraid'de
  `/mnt/user/appdata`, TrueNAS'ta `/mnt/<havuz>` altında bir veri kümesi) ve yeni bir klonun
  içinden `./install.sh --platform nas --no-start` çalıştırın, ya da tek satırlık komutu
  `--dir <o klasör>/healtos` ile çalıştırın.
- *NAS'ta Git yoksa:* başka bir bilgisayarda depoyu klonlayın ve
  `./install.sh --platform nas --dry-run --env-out ./nas.env` çalıştırın. Klonu NAS paylaşımına
  kopyalayın, `nas.env` dosyasını orada `.env` adıyla kaydedin. İçindeki gizli anahtarlar
  gerçektir; dosya yolları (örneğin Garmin anahtar dosyası) çalıştırıldığı bilgisayarı gösterir,
  NAS için düzeltin.

**2. adım: projeyi başlatın.**

- **Synology DSM 7.2+ (Container Manager):** *Proje* › *Oluştur* › yol olarak klon klasörünü
  seçin ve *Var olan docker-compose.yml dosyasını kullan* deyin. Container Manager uygulama
  imajını derler ve `app` ile `postgres` kapsayıcılarını başlatır.
- **QNAP (Container Station 3):** *Uygulamalar* › *Oluştur*; çalışma dizini klon klasörü olacak
  şekilde `docker-compose.yml` dosyasını yapıştırın ya da yükleyin.
- **Unraid:** *Docker Compose Manager* eklentisini kurun, dizini klon olan bir yığın ekleyin,
  sonra *Compose Up*.
- **TrueNAS SCALE 24.10+:** Uygulamalar Docker ile çalışır; *Discover Apps* › *Custom App* ›
  *Install via YAML* bir Compose dosyası alır, ama klondan derlemek SSH ile daha kolaydır: klonun
  içinde `sudo docker compose up -d --build`.

NAS arayüzleri genellikle yalnızca `docker-compose.yml` dosyasını okur ve `.env` içindeki
`COMPOSE_FILE` satırını yok sayar. Deneme kipi (`docker-compose.dev.yml`) ya da Garmin
(`docker-compose.garmin.yml`) için yığını SSH ile, klonun içinden başlatın; orada
`docker compose up -d --build` `COMPOSE_FILE` satırına uyar (Synology ve QNAP'ta `sudo` ile).
İki yol da aynı birimi kullanır.

**3. adım: yedekler.** `scripts/backup.sh` NAS'ta bash, Python 3.11+ ve `age` (ya da `gpg`)
ister. Çoğu NAS Python ile gelir ama `age` ile gelmez; `age` için
[resmi sürümdeki](https://github.com/FiloSottile/age/releases) tek dosyalık ikili dosyayı `PATH`
üzerindeki bir dizine koyun. Anahtarı bir kez root olarak oluşturun ve NAS dışına kopyalayın:

```sh
sudo sh -c 'umask 077; mkdir -p /root/.config/healtos && age-keygen -o /root/.config/healtos/backup.key'
```

Sonra şu komutu NAS'ın kendi zamanlayıcısıyla her gün 03:30'da root olarak çalıştırın:

```sh
cd <klon> && BACKUP_KEY_FILE=/root/.config/healtos/backup.key /bin/bash scripts/backup.sh
```

Synology: *Denetim Masası* › *Görev Zamanlayıcı* › *Kullanıcı tanımlı komut dosyası*, kullanıcı
root; QNAP: SSH ile `crontab -e`; Unraid: *User Scripts* eklentisi; TrueNAS: *System* ›
*Advanced* › *Cron Jobs*. `.env` içinde
`BACKUP_HOST_DIR` değerini başka bir birimdeki ya da kendisi yedeklenen bir paylaşımdaki klasöre
ayarlayın.

**4. adım: pano.** Linux'taki gibi uygulama NAS'ın `127.0.0.1` adresine bağlıdır; panoyu
[SSH tüneliyle](#panoyu-başka-bir-bilgisayardan-açmak) açın. 8000 portunu yerel ağa açmayın.

### macOS

**Gerekenler:** Apple Silicon ya da Intel üzerinde macOS; bir Docker çalışma ortamı: [Docker
Desktop](https://docs.docker.com/desktop/setup/install/mac-install/), [OrbStack](https://orbstack.dev/)
ya da [Colima](https://github.com/abiosoft/colima) (`brew install colima docker docker-compose`);
Git (`xcode-select --install` ya da Homebrew); yedekler için `brew install age` ve Python 3.11+
(`brew install python`; Xcode komut satırı araçlarındaki `python3` çok eski).

```sh
curl -fsSL https://raw.githubusercontent.com/M3rgen/HealtOS/main/install.sh | bash -s -- --lang tr
```

Kurulum aracı macOS'u algılar ve önceden seçer. Kurulum dizini: `~/healtos`. Yedek anahtarı:
`~/Library/Application Support/HealtOS/backup.key`.

- **Yedekler ve köprü** kullanıcınızın launchd ajanları olarak çalışır: `org.healtos.backup`
  (her gün 03:30) ve `org.healtos.bridge` (isteğe bağlı); `deploy/launchd/` içindeki şablonlardan
  `~/Library/LaunchAgents/` altına kurulur. Listelemek için `launchctl list | grep -i healtos`;
  hemen bir yedek almak için `launchctl kickstart gui/$(id -u)/org.healtos.backup`. Günlükler:
  `~/Library/Logs/HealtOS/`.
- **Otomatik açılış:** Docker Desktop: *Settings* › *General* › *Start Docker Desktop when you
  sign in*. OrbStack varsayılan olarak oturum açılınca başlar. Colima: `brew services start colima`.
  Kapsayıcılar sonra kendiliğinden geri gelir.
- **Uyku:** uyuyan bir Mac ne eşitleme yapar ne yedek alır. launchd, kaçırılan 03:30 yedeğini
  uyandıktan sonra çalıştırır, ama kapalı bir Mac için çalıştırmaz. HealtOS'u gece gündüz
  çalıştıracak bir Mac için *Sistem Ayarları* › *Enerji* altında otomatik uykuyu kapatın.

### Windows 10 ve 11

**Gerekenler:** Windows 10 22H2 ya da Windows 11; WSL 2 altyapılı [Docker
Desktop](https://docs.docker.com/desktop/setup/install/windows-install/) (varsayılan; `wsl --install`
WSL 2'yi kurar); [Git for Windows](https://git-scm.com/download/win) ya da `winget install Git.Git`;
yedekler için `winget install FiloSottile.age`. Windows PowerShell 5.1 ve PowerShell 7'nin ikisi
de çalışır.

```powershell
irm https://raw.githubusercontent.com/M3rgen/HealtOS/main/install.ps1 | iex
```

Kurulum aracı Windows'u algılar ve önceden seçer. Bütün platformları listeler ama bu bilgisayara
yalnızca Windows kurulumunu yapar; başka bir seçim için o makinede çalıştırılacak komutu yazar.
Kurulum dizini: `%USERPROFILE%\healtos`. Yedek anahtarı: `%APPDATA%\HealtOS\backup.key`,
yalnızca sizin hesabınız okuyabilir. Git ya da Docker Desktop yoksa ya da Docker motoru
çalışmıyorsa `winget` komutunu ya da çözümü yazar ve çıkış kodu 2 ile durur.

- **Yedekler** kullanıcınızın `\HealtOS\HealtOS Backup` Görev Zamanlayıcı görevi olarak her gün
  03:30'da `scripts\backup.ps1` ile çalışır (`scripts/backup.sh` ile aynı davranış). Görevleri
  `Get-ScheduledTask -TaskPath '\HealtOS\'` ile listeleyin; hemen bir yedek için
  `Start-ScheduledTask -TaskPath '\HealtOS\' -TaskName 'HealtOS Backup'`. Günlükler:
  `%LOCALAPPDATA%\HealtOS\logs\`.
- **Otomatik açılış:** Docker Desktop ancak oturum açtıktan sonra başlar. Kurulum aracı
  [`scripts/enable-docker-desktop-autostart.ps1`](../scripts/enable-docker-desktop-autostart.ps1)
  betiğini çalıştırır; bu, Docker Desktop'ı kullanıcınızın başlangıç girdilerine ekler.
  Kapsayıcılar sonra `restart: unless-stopped` ile geri gelir. Biri oturum açana kadar HealtOS
  çalışmaz.
- **Umay köprüsü** (isteğe bağlı) oturum açılınca `\HealtOS\HealtOS Umay bridge` zamanlanmış
  görevi olarak çalışır; Codex CLI
  kurulumu için [Windows notlarına](legacy/WINDOWS.md#umay-bridge-on-windows) bakın.
- **Yerel kopyayı çalıştırmak:** PowerShell yürütme ilkesi yüzünden `.\install.ps1` komutunu
  reddederse `powershell -ExecutionPolicy Bypass -File .\install.ps1` olarak çalıştırın. Bu hiçbir
  ayarı değiştirmez.
- Klonu OneDrive ile eşitlenen bir klasörde değil, yerel bir NTFS sürücüsünde tutun.

### Bulut VPS

VPS, kiralık bir sunucudaki [Linux](#linux-sunucu-ya-da-sanal-makine-varsayılan) kurulumudur;
*Bulut VPS* seçin ya da `--platform vps` verin. Fark, sunucunun internette olmasıdır:

- Uygulama `127.0.0.1` adresine bağlıdır ve PostgreSQL hiçbir port açmaz; böyle kalsın. Sağlayıcının
  güvenlik duvarında 8000 portunu açmayın. Panoya yalnızca
  [SSH tüneliyle](#panoyu-başka-bir-bilgisayardan-açmak) ulaşın.
- Gelen bağlantılarda yalnızca SSH'ye izin verin (tercihen yalnızca anahtarla); gerisi kapalı kalır.
- Google webhook'ları için yalnızca webhook geçiren [Cloudflare Tunnel](CLOUDFLARE_TUNNEL.md)
  kurulumunu kullanın: o da gelen port istemez.
- Yedek anahtarını ve yedeklerin kopyalarını VPS dışında bir yerde saklayın.

## Veri kaynakları

İstediğiniz birleşimi seçin. Sonradan kaynak eklemek için kurulum aracını yeni `--sources` ile
yeniden çalıştırın: kaynağın ayarlarını doldurur, `…_ENABLED` anahtarını açar ve Compose dosyasını
`COMPOSE_FILE` satırına ekler. Bir kaynağın sakladığı hiçbir şey silinmez.

| Kaynak | Cihazlar | Gereken | Varsayılan |
| --- | --- | --- | --- |
| Google Health API v4 | Fitbit, Pixel Watch, Google Health uygulaması | Uygun bir Google Cloud projesi, OAuth web istemcisi | yapılandırılana kadar kapalı |
| Garmin Connect Personal API | Garmin saatleri ve cihazları | Personal API anahtar dosyası | kapalı (`GARMIN_ENABLED=false`) |
| Apple Sağlık | Apple Watch, iPhone | `export.zip` için hiçbir şey; otomatik yükleme için Health Auto Export uygulaması | yükleme hazır; alıcı kapalı |
| Yok | — | Hiçbir şey; elle girişler ve boş pano | — |

### Google Health API v4 (Fitbit, Pixel Watch)

Google, 3 Ekim 2026 itibarıyla yeni Google Health API projelerinin kabul edilmediğini söylüyor;
önce [resmi kurulum rehberine](https://developers.google.com/health/setup) bakın.

1. Kurulum aracının yazdığı yönlendirme adresiyle bir **Web application** OAuth istemcisi
   oluşturun: `http://localhost:8000/auth/google/callback` (SSH tüneli üzerinden de çalışır).
2. İstemci kimliğini ve gizli anahtarını kurulum aracına verin (ya da sonra `.env` dosyasına
   yazın). `GOOGLE_TOKEN_ENCRYPTION_KEY` anahtarını kendisi üretir. Bu anahtarı yedek anahtarınızla
   birlikte saklayın: geri yüklenen bir veritabanı, saklanan Google belirteçlerini okumak için onu
   ister.
3. <http://localhost:8000/auth/google/start> adresini açıp izni verin, sonra:

   ```sh
   docker compose exec app health-mirror probe --days 30
   docker compose exec app health-mirror backfill
   ```

Tam kip ayrıca `GOOGLE_HEALTH_WEBHOOK_ENDPOINT_URI` (HTTPS) ve `GOOGLE_HEALTH_WEBHOOK_AUTHORIZATION`
ister; bkz. [dağıtım 7. adım](DEPLOYMENT.md#7-webhook-through-cloudflare-tunnel). Kapsamlar ve
Testing kipindeki yedi günlük belirteç sınırı: [OAuth başvurusu](REFERENCE.md#oauth-and-google-health-setup).

### Garmin Connect Personal API

Kendi Garmin Connect hesabınız için salt okunur, yalnızca çekme yapan bir kaynak.

1. Garmin Connect'te bir Personal API anahtarı oluşturun ve tek satır olarak bir dosyaya
   kaydedin (varsayılan `~/.config/garmin/connect-personal-api-key`) ya da kurulum aracı sorunca
   yapıştırın, dosyayı o yazsın:

   ```sh
   mkdir -p ~/.config/garmin && chmod 700 ~/.config/garmin
   ( umask 077; cat > ~/.config/garmin/connect-personal-api-key )   # anahtarı yapıştırın, sonra Ctrl-D
   ```

2. Kurulum aracında Garmin'i seçin (ya da `--sources garmin --garmin-key-file YOL`).
   `GARMIN_ENABLED=true`, `GARMIN_API_KEY_HOST_FILE` ve `GARMIN_API_KEY_HOST_GID` yazar ve
   `COMPOSE_FILE` satırına `docker-compose.garmin.yml` ekler; bu ek dosya anahtar dosyasını
   uygulamaya salt okunur bağlar. Uygulama kapsayıcısı kendi kullanıcısıyla (uid 10001) çalıştığı
   için kurulum aracı Linux'ta dosyayı okunur yapar. root iken grubu 10001, modu 640 yapar. Birincil
   grubu paylaşılan olan normal kullanıcıda (1000'in altında bir grup kimliği, örneğin `users`, ya da
   başka üyeleri olan bir grup) dosyayı **gevşetmez**; çalıştırmanız gereken komutu
   (`sudo chgrp 10001 DOSYA && sudo chmod 640 DOSYA`) yazdırır ve komutu çalıştırıp kurulum aracını
   yeniden çalıştırana kadar Garmin'i kapalı bırakır (bayrağa dokunmaz). Özel birincil grupta dosyayı
   grubunca okunur yapar (mod 640) ve o grubu kapsayıcıya ekler. Anahtarın kendisi asla `.env` dosyasına, veritabanına ya da bir günlüğe girmez. Dosya
   yoksa kurulum aracı Garmin'i kapalı bırakır ve sonraki adımı söyler; anahtarı oraya kaydedip
   `--sources garmin` ile yeniden çalıştırın. `--garmin-key-file YOL` açıkça verilmişse ve dosya yoksa
   kurulum aracı bunun yerine çıkış kodu 2 ile durur. macOS'ta Colima ile sanal makine yalnızca
   kullanıcınızın okuyabildiği bir dosyayı okuyamayabilir; uygulama anahtarı okuyamıyorsa dosya
   izinlerini ve Colima bağlamalarını (`colima start --mount`) denetleyin ya da Docker Desktop ya da
   OrbStack kullanın. Aynı kurallar Apple alıcısı belirteç dosyası için de geçerlidir. Windows'ta varsayılan yol `%USERPROFILE%\.config\garmin\connect-personal-api-key`.
3. Denetim: `docker compose exec app health-mirror garmin-status`. Daha eski geçmiş:
   `docker compose exec app health-mirror garmin-backfill --since 2026-06-01`.

Anahtarın süresi sınırlıdır; yenilemek için dosyanın içeriğini değiştirin. Ayrıntılar:
[Garmin başvurusu](REFERENCE.md#garmin-connect-personal-api).

### Apple Watch / Apple Sağlık

- **`export.zip` yüklemek kurulum istemez.** iPhone'da: Sağlık › profil resmi › *Tüm Sağlık
  Verilerini Dışa Aktar*; `export.zip` dosyasını panoyu açtığınız bilgisayara aktarın ve Sistem
  sayfasında (“Apple Sağlık içe aktar”) yükleyin. Sonradan yüklenen daha yeni bir dışa aktarım
  yalnızca yeni kayıtları ekler.
- **Health Auto Export alıcısı (isteğe bağlı, varsayılan kapalı).** Alıcı için evet deyin (ya da
  `--apple-receiver yes`). Kurulum aracı `APPLE_RECEIVER_ENABLED=true` yazar, alıcı belirtecini
  `~/.config/healthos/apple-receiver-token` dosyasında oluşturur (Windows'ta
  `%USERPROFILE%\.config\healthos\apple-receiver-token`; var olan dosya korunur, asla
  yenilenmez), onu `docker-compose.apple.yml` ile salt okunur bağlar ve uygulamanın REST API
  otomasyonu için `Authorization: Bearer …` başlığını bir kez yazdırır. Yalnızca
  `POST /webhooks/apple-health` yolunu kendi HTTPS tünelinizden geçirin ve
  `APPLE_RECEIVER_ENDPOINT_URI` değerini o adrese ayarlayın (kurulum aracı sorar). Bkz.
  [README](../README.md#optional-apple-watch--apple-health) ve
  [Apple başvurusu](REFERENCE.md#apple-health-github-version).

### Şimdilik kaynak yok

Boş pano ve elle girişlerle başlamak için hiçbirini seçmeyin (`--sources none`). Sonradan kaynak
eklemek için [yukarıya](#veri-kaynakları) bakın.

## Yedekler

Her platform [`scripts/backup.sh`](../scripts/backup.sh) ile aynı yedek davranışını kullanır:
veritabanının bir `pg_dump` dökümü, diske değmeden önce `age` ile şifrelenir ve
`BACKUP_HOST_DIR` (varsayılan klondaki `./backups`) altına `health-<UTC zaman damgası>.dump.age`
olarak yazılır.

- **Sonsuza kadar saklanır.** `BACKUP_RETENTION_DAYS` değerini kendiniz pozitif bir gün sayısına
  ayarlamadıkça hiçbir şey silinmez. Diskte yer azsa çalıştırma başlamayı reddeder; yer açmak için
  eski yedekleri asla silmez.
- **Durum:** her çalıştırma `last-backup-status.json` yazar; pano bunu `GET /health/backup`
  üzerinden okur.
- **Anahtar**, klonun ve `.env` dosyasının dışında duran bir `age` kimliğidir. Kurulum aracı
  oluşturmayı önerir:

  | Platform | Anahtar dosyası | Zamanlayıcı | Hemen bir yedek al |
  | --- | --- | --- | --- |
  | Linux (root), Raspberry Pi, VPS | `/etc/healthos/backup.key` (root, mod 600) | systemd zamanlayıcısı | `sudo systemctl start healthos-backup.service` |
  | Linux (kendi kullanıcınız) | `~/.config/healtos/backup.key` (mod 600) | systemd kullanıcı zamanlayıcısı | `systemctl --user start healthos-backup.service` |
  | NAS | `/root/.config/healtos/backup.key` (`age-keygen -o` ile oluşturun) | NAS görev zamanlayıcısı | `BACKUP_KEY_FILE=/root/.config/healtos/backup.key bash scripts/backup.sh` |
  | macOS | `~/Library/Application Support/HealtOS/backup.key` | launchd ajanı | `launchctl kickstart gui/$(id -u)/org.healtos.backup` |
  | Windows | `%APPDATA%\HealtOS\backup.key` (yalnızca sizin hesabınız) | Görev Zamanlayıcı | `powershell -ExecutionPolicy Bypass -File .\scripts\backup.ps1` |

- **Anahtarın bir kopyasını makinenin dışında saklayın**, örneğin bir parola yöneticisinde,
  `GOOGLE_TOKEN_ENCRYPTION_KEY` ile birlikte. Anahtar olmadan hiçbir yedek okunamaz. Yalnızca
  makinede duran bir anahtar, makineyle birlikte kaybolur.
- Daha güçlü seçenek: kimliği başka bir makinede oluşturun (`age-keygen -o healtos-backup.age`) ve
  makineye yalnızca açık anahtarını `BACKUP_AGE_RECIPIENT` olarak koyun (Linux'ta
  `/etc/healthos/backup.env` içinde). O zaman çalınan bir makine kendi yedeklerini okuyamaz.
- **Yedeklerin kendisini de makinenin dışına kopyalayın**, zaten kullandığınız yöntemle; HealtOS
  makine dışı kopya yapmaz.
- **Bir yedeği kanıtlayın:** canlı veritabanına hiç dokunmayan geri yükleme tatbikatıyla:
  `make restore FILE=backups/health-<damga>.dump.age` (Windows:
  `powershell -ExecutionPolicy Bypass -File .\scripts\restore.ps1 backups\health-<damga>.dump.age`). Bkz.
  [işletim rehberi](RUNBOOK.md#backup-verify-and-restore-drill).

## Güncelleme

Güncelleme her platformda aynıdır: yedek alın, çekin, kurulum aracını yeniden çalıştırın.

```sh
cd ~/healtos                 # ya da /opt/healthos, ya da %USERPROFILE%\healtos
make backup                  # ya da: bash scripts/backup.sh  (Windows: .\scripts\backup.ps1)
git pull --ff-only
./install.sh --lang tr       # Windows: .\install.ps1 -Lang tr
```

Önceki yanıtların aynısını (kip, kaynaklar) verin ya da aynı seçenekleri geçin, çünkü
`COMPOSE_FILE` son çalıştırmanın seçimlerini izler.

Kurulum aracını yeniden çalıştırmak güvenlidir: önce `.env` dosyasının tarihli bir kopyasını
alır, her gizli anahtarı korur, yeni sürümlerin eklediği ayarları doldurur, zamanlanmış
işleri yeniler ve yığını yeniden derler; uygulama başlarken veritabanını taşır (migration).
Kurulum aracı olmadan `docker compose up -d --build` yeniden derlemeyi yapar. 03:30 yedek
penceresinin dışında güncelleyin. Sürüm notları: [CHANGELOG](../CHANGELOG.md); migration
ayrıntıları: [işletim rehberi](RUNBOOK.md#update-the-application).

Bir sürümde kalmak için kurulum aracını çalıştırmadan önce onun etiketine geçin
(`git fetch --tags && git checkout v0.2.2`).

## Veri kaybetmeden durdurma ve kaldırma

Arşiviniz `postgres_data` Docker biriminde, yedekleriniz `BACKUP_HOST_DIR` altında durur. Aşağıdaki
adımların hiçbiri bunları silmez.

```sh
docker compose stop          # durdurur; `docker compose start` sürdürür
docker compose down          # kapsayıcıları ve ağı kaldırır; birim kalır
```

**`docker compose down -v` ya da `docker volume rm` asla çalıştırmayın:** bunlar veritabanı
birimini ve onunla birlikte bir yedekte olmayan her kaydı siler. Kurulum aracı ikisini de asla
yapmaz.

HealtOS'u bir makineden, verileri koruyarak kaldırmak için:

1. Son bir yedek alın; onu, yedek anahtarını ve `.env` dosyasını makinenin dışına kopyalayın.
2. `docker compose down` (`-v` olmadan).
3. Zamanlanmış işleri kapatın:
   - Linux: `sudo systemctl disable --now healthos-backup.timer healthos-bridge.service`
     (kullanıcı birimleri: `systemctl --user disable --now …`).
   - macOS: `launchctl bootout gui/$(id -u)/org.healtos.backup` (ve `org.healtos.bridge`);
     `~/Library/LaunchAgents/` altındaki `.plist` dosyaları kalabilir.
   - Windows: `Disable-ScheduledTask -TaskPath '\HealtOS\' -TaskName 'HealtOS Backup'` (ve
     `'HealtOS Umay bridge'`), ya da Görev Zamanlayıcı'da *Devre Dışı Bırak*.
4. Klonu, `.env` dosyasını, `.env.bak-*` kopyalarını ve `backups` klasörünü yerinde bırakın ya da
   bir arşive taşıyın. `postgres_data` birimi siz başka bir karar verene kadar Docker'da kalır.

Aynı klonda kurulum aracını sonra yeniden çalıştırmak aynı `.env` dosyasını ve aynı birimi
kullanır.

## Sorun giderme

**Her platform**

| Belirti | Neden ve çözüm |
| --- | --- |
| `required variable POSTGRES_PASSWORD is missing` | `docker compose` komutunu çalıştırdığınız dizinde `.env` yok; klonun içine `cd` ile geçin. |
| Uygulama `unsafe or missing production configuration: <adlar>` ile yeniden başlıyor | Tam kip, ama üretim değerlerinin hepsi yok. Adları `.env` dosyasına yazıp kurulum aracını yeniden çalıştırın ya da o zamana kadar `--mode trial` kullanın. |
| Giriş sayfası anahtarı kabul etmiyor | `.env` içindeki `APP_UI_TOKEN` değerini tırnaksız, aynen girin. |
| 8000 portu kullanımda | `.env` içinde `APP_PORT` ayarlayıp `docker compose up -d` çalıştırın. Google'ın yönlendirme adresi 8000 portunda kalır; Google kullanıyorsanız portu boşaltmak daha iyidir. |
| `/health` `configuration_incomplete` listeliyor | Google bağlanıp webhook değerleri girilene kadar beklenen durum. |
| Yedek durumu `failed` | `BACKUP_HOST_DIR` içindeki `last-backup-status.json` dosyasını okuyun; olağan nedenler eksik anahtar, kurulu olmayan `age` ya da yetersiz boş disk alanıdır. |
| Güncellemeden sonra bir şey yanlış görünüyor | `docker compose logs --tail=200 app`. Önceki `.env` `.env.bak-<damga>` dosyasındadır. |

**Linux, Raspberry Pi, VPS**

- `permission denied … docker.sock`: kullanıcınızı `docker` grubuna ekleyin
  (`sudo usermod -aG docker "$USER"`, sonra yeniden oturum açın) ya da kurulum aracını root olarak
  çalıştırın.
- Yedek zamanlayıcısı yok: `systemctl list-timers | grep healthos` (kullanıcı birimleri:
  `systemctl --user list-timers`); `journalctl -u healthos-backup.service -n 50` çıktısına bakın.
- Kullanıcı birimleri oturumu kapatınca duruyor: kalıcılık kapalı; `loginctl enable-linger "$USER"`
  çalıştırın.
- Raspberry Pi'de derleme öldürüldü (`exit code 137`): bellek yetmedi; takas alanı ekleyin ya da
  4 GB+ bir kart kullanın.
- `exec format error`: 32 bit işletim sistemi ya da başka mimari için derlenmiş imaj; 64 bit
  işletim sistemi kullanıp `docker compose build --no-cache app` ile yeniden derleyin.

**NAS**

- Arayüzden başlatılan proje üretim kipinde başlayıp duruyor: arayüz `COMPOSE_FILE` satırını yok
  saydı; yığını [2. adımda](#nas-synology-qnap-unraid-truenas-scale) anlatıldığı gibi SSH ile
  başlatın.
- Synology/QNAP'ta SSH üzerinden `docker: permission denied`: `sudo docker compose …` kullanın.

**macOS**

- `Cannot connect to the Docker daemon`: Docker Desktop'ı, OrbStack'i ya da `colima start` ile
  Colima'yı başlatın.
- Colima derleme sırasında belleği tüketiyor: `colima start --memory 4`.
- Bir launchd ajanı çalışmıyor: `launchctl print gui/$(id -u)/org.healtos.backup` durumunu ve son
  çıkış kodunu gösterir; günlük yolu `~/Library/LaunchAgents/org.healtos.backup.plist` içinde
  yazar.

**Windows**

- `docker` tanınmıyor: Docker Desktop'ı kurduktan sonra yeni bir PowerShell penceresi açın ve
  Docker Desktop'ı bir kez başlatın.
- Docker Desktop'ta WSL 2 hataları: yönetici olarak `wsl --update` çalıştırıp yeniden başlatın.
- Betik yürütme ilkesine takılıyor: `powershell -ExecutionPolicy Bypass -File .\install.ps1`
  kullanın.
- Yedek görevi var ama bir şey olmuyor: Görev Zamanlayıcı'da görevin *Geçmiş* sekmesini açın ya
  da hatayı görmek için `.\scripts\backup.ps1` betiğini elle çalıştırın.

Daha fazlası: [teknik başvuru](REFERENCE.md), [işletim rehberi](RUNBOOK.md),
[dağıtım rehberi](DEPLOYMENT.md).
