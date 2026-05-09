# ELLE Reg-Bot

CLI tool để quản lý pipeline đăng ký tài khoản, theo dõi mail xác nhận, verify link, export account đã verified và generate Gmail alias trực tiếp vào SQLite DB.

Bot dùng **Python + Camoufox**. Không cần Chrome/Chromium cài sẵn.

## Tính năng chính

- Terminal UI qua `main.py` / `run.ps1` / `run.sh`.
- Tự tạo virtual environment `.venv` và cài dependency bằng installer.
- Tạo `.env` từ `.env.example` nếu chưa có, **không overwrite `.env` cũ**.
- Generate alias Gmail và import thẳng vào `accounts.db`, không cần `accounts.txt`.
- Register batch nhiều worker, mỗi worker dùng profile Camoufox riêng.
- Mail listener chạy chung với batch theo mặc định.
- Verify link trong mail bằng HTTP async với concurrency tự tính từ `BATCH_WORKERS`.
- Export account `verified` ra `.txt` rồi đánh dấu `exported_verified` để không export trùng.

## Yêu cầu

- Python **3.11+**
- Internet để cài package và fetch Camoufox browser assets
- Gmail App Password nếu dùng Gmail IMAP

Kiểm tra Python:

```powershell
py -3 --version
```

```bash
python3 --version
```

## Cài đặt trên Windows

Mở PowerShell tại thư mục project, rồi chạy:

```powershell
cd Reg-Bot
powershell -ExecutionPolicy Bypass -File .\install.ps1
notepad .env
.\run.ps1
```

Nếu chỉ muốn mở file `.env` sau này:

```powershell
.\run.ps1 -EditEnv
```

Chạy lại bot hằng ngày:

```powershell
cd Reg-Bot
.\run.ps1
```

## Cài đặt trên Linux

```bash
cd Reg-Bot
chmod +x install.sh run.sh
./install.sh
nano .env
./run.sh
```

Nếu chỉ muốn mở file `.env` sau này:

```bash
./run.sh --edit-env
```

Chạy lại bot hằng ngày:

```bash
cd Reg-Bot
./run.sh
```

Trên Linux server không có màn hình, set:

```env
HEADLESS="true"
```

## Cấu hình `.env`

Installer sẽ tạo `.env` từ `.env.example` nếu chưa có. File `.env` được load mỗi lần chạy, nên có thể sửa runtime mà không cần cài lại.

Các biến quan trọng:

```env
# Thông tin form đăng ký fallback / single-run
ELLE_USERNAME="ten-dang-nhap"
ELLE_EMAIL="your-alias@example.com"
ELLE_PASSWORD="ChangeMe123!"

# Browser
HEADLESS="false"
SLOW_MO_MS="50"

# Gmail IMAP — dùng App Password 16 ký tự
IMAP_USER="yourname@gmail.com"
IMAP_PASS="xxxx xxxx xxxx xxxx"
IMAP_FOLDER="INBOX"
IMAP_FROM="elledigital@3480726.brevosend.com"
IMAP_POLL_SEC="5"

# Batch register
BATCH_WORKERS="5"
BATCH_SIZE="10"
BATCH_SLEEP_SEC="5"
BATCH_VERIFY_WAIT_SEC="120"
BATCH_LOG_LEVEL="normal"

# Verify link trong mail
VERIFY_TIMEOUT_SEC="15"
# VERIFY_CONCURRENCY="8"  # optional override
```

Gợi ý:

- `BATCH_WORKERS`: số process Camoufox chạy song song.
- `BATCH_SIZE`: số account mỗi worker xử lý trước khi restart context.
- `BATCH_SLEEP_SEC`: delay giữa mỗi account, tăng nếu gặp nhiều 429.
- `BATCH_LOG_LEVEL="quiet"`: giảm log terminal.
- `VERIFY_CONCURRENCY`: không cần set nếu muốn auto tính từ `BATCH_WORKERS`.

## Dùng Terminal UI

Chạy launcher:

```powershell
.\run.ps1
```

```bash
./run.sh
```

Menu:

```text
1) Register batch
2) Mail verifier only
3) Export verified → txt
4) Generate aliases → DB
5) Show DB stats
6) Reset stuck registering
0) Exit
```

Luồng thường dùng:

1. Chọn `4) Generate aliases → DB` để thêm alias vào database.
2. Chọn `5) Show DB stats` để kiểm tra số `pending`.
3. Chọn `1) Register batch` để chạy đăng ký + mail listener.
4. Chọn `3) Export verified → txt` để xuất account đã verified.

## Chạy nhiều worker / nhiều PowerShell

Khuyến nghị: dùng **1 terminal** và tăng `BATCH_WORKERS` trong `.env`.

```env
BATCH_WORKERS="5"
```

Vẫn có thể mở nhiều PowerShell/terminal nếu cần:

- SQLite claim account bằng transaction atomic, tránh 2 worker lấy cùng 1 email.
- DB bật WAL + busy timeout để giảm lỗi `database is locked`.
- Mỗi lần chạy batch dùng Camoufox profile prefix có PID riêng, tránh tranh profile browser.

Lưu ý: tổng concurrency = tổng worker của tất cả terminal. Ví dụ 2 terminal x `BATCH_WORKERS=5` = 10 browser song song, dễ gặp rate limit/429 hơn.

## Chạy script riêng lẻ

Nếu không dùng Terminal UI:

```powershell
.\.venv\Scripts\python.exe scripts\register_batch.py --total 100
.\.venv\Scripts\python.exe scripts\check_elle_mail.py
.\.venv\Scripts\python.exe scripts\accounts_db.py stats
.\.venv\Scripts\python.exe scripts\accounts_db.py export-verified --out output\verified-accounts.txt
```

Linux tương đương:

```bash
./.venv/bin/python scripts/register_batch.py --total 100
./.venv/bin/python scripts/check_elle_mail.py
./.venv/bin/python scripts/accounts_db.py stats
./.venv/bin/python scripts/accounts_db.py export-verified --out output/verified-accounts.txt
```

## File quan trọng

| File / thư mục               | Mục đích                           |
| ---------------------------- | ---------------------------------- |
| `main.py`                    | Terminal UI chính                  |
| `install.ps1`                | Installer Windows                  |
| `install.sh`                 | Installer Linux                    |
| `run.ps1`                    | Launcher Windows                   |
| `run.sh`                     | Launcher Linux                     |
| `.env.example`               | Template config public             |
| `.env`                       | Config runtime riêng, không commit |
| `accounts.db`                | SQLite DB chứa account/status      |
| `output/`                    | File export verified               |
| `camoufox-profile*/`         | Browser profile runtime            |
| `scripts/accounts_db.py`     | DB helper + CLI                    |
| `scripts/register_batch.py`  | Batch register + worker pool       |
| `scripts/check_elle_mail.py` | IMAP listener + verify link        |
| `scripts/gen_aliases.py`     | Generate Gmail aliases             |
| `scripts/register_elle.py`   | Flow đăng ký bằng Camoufox         |

## Runtime files không nên commit

Các file/thư mục này đã được ignore:

```text
.env
.venv/
accounts.db
accounts.db-wal
accounts.db-shm
accounts.txt
camoufox-profile*/
output/
__pycache__/
```

## Troubleshooting

### PowerShell chặn script

Chạy installer bằng:

```powershell
powershell -ExecutionPolicy Bypass -File .\install.ps1
```

### Thiếu Camoufox browser assets

Chạy lại:

```powershell
.\.venv\Scripts\python.exe -m camoufox fetch
```

```bash
./.venv/bin/python -m camoufox fetch
```

### Gmail không đọc được mail

- Bật IMAP trong Gmail.
- Dùng Gmail App Password, không dùng mật khẩu Gmail thường.
- Kiểm tra `IMAP_USER`, `IMAP_PASS`, `IMAP_FOLDER`, `IMAP_FROM` trong `.env`.

### Nhiều `registering` bị kẹt

Trong Terminal UI chọn:

```text
6) Reset stuck registering
```

Hoặc chạy:

```powershell
.\.venv\Scripts\python.exe scripts\accounts_db.py reset-stuck --max-age-min 15
```

### Gặp nhiều 429 / rate limit

Giảm concurrency:

```env
BATCH_WORKERS="2"
BATCH_SLEEP_SEC="10"
```

Sau đó chạy lại `run.ps1` / `run.sh`.

## Verify nhanh sau khi setup

Windows:

```powershell
.\.venv\Scripts\python.exe -c "from camoufox.sync_api import Camoufox; print('camoufox ok')"
.\run.ps1
```

Linux:

```bash
./.venv/bin/python -c "from camoufox.sync_api import Camoufox; print('camoufox ok')"
./run.sh
```
