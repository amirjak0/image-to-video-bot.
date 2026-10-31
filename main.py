import os
import shutil
import logging
import re
import urllib.request
import json
import subprocess
import time
import tempfile
import io
from datetime import datetime, timezone, timedelta
from gradio_client import Client, handle_file

# ================= اضافه شدن کتابخانه‌های گوگل درایو =================
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload

# ================= تنظیمات منطقه زمانی ایران (UTC+3:30) =================
IRAN_TZ = timezone(timedelta(hours=3, minutes=30))

class IranTimeFormatter(logging.Formatter):
    def formatTime(self, record, datefmt=None):
        dt = datetime.fromtimestamp(record.created, tz=IRAN_TZ)
        if datefmt:
            return dt.strftime(datefmt)
        return dt.strftime('%Y-%m-%d %H:%M:%S')

# ================= تنظیمات پوشه‌ها و فایل‌ها =================
log_file = 'system_log.txt'
IP_FILE = 'used_ips.txt'
SEED_STATE_FILE = 'seed_state.txt'

# ================= شناسه پوشه گوگل درایو برای دریافت عکس =================
GDRIVE_IMAGE_FOLDER_ID = "17nyIKNPWugzCHXiTkuUIyfWf1QyhmLZK"

# ================= محدودسازی حجم فایل لاگ =================
MAX_LOG_SIZE = 150 * 1024   # ۱۵۰ کیلوبایت
KEEP_LOG_SIZE = 30 * 1024   # بعد از پاک‌سازی، ۳۰ کیلوبایت آخر نگه داشته شود

def trim_log_if_needed(log_path):
    if os.path.exists(log_path) and os.path.getsize(log_path) > MAX_LOG_SIZE:
        try:
            with open(log_path, 'r', encoding='utf-8', errors='ignore') as f:
                content = f.read()
            with open(log_path, 'w', encoding='utf-8') as f:
                f.write(content[-KEEP_LOG_SIZE:])
        except Exception:
            with open(log_path, 'w', encoding='utf-8') as f:
                f.write("")

# ================= تنظیمات لاگ‌گیری =================
trim_log_if_needed(log_file)

logger = logging.getLogger()
logger.setLevel(logging.INFO)

if logger.hasHandlers():
    logger.handlers.clear()

file_handler = logging.FileHandler(log_file, encoding='utf-8')
file_handler.setFormatter(IranTimeFormatter('%(asctime)s - %(levelname)s - %(message)s'))
logger.addHandler(file_handler)

console_handler = logging.StreamHandler()
console_handler.setFormatter(IranTimeFormatter('%(asctime)s - %(levelname)s - %(message)s'))
logger.addHandler(console_handler)

# ================= سرورهای اختصاصی =================
ALL_SERVERS = [
    "r3gm/wan2-2-fp8da-aoti-preview"
]

MAX_WAIT_LIMIT_SECONDS = 900

# ================= 🎬 بارگذاری اکشن‌ها از سکرت‌ها =================
ACTIONS_JSON_STR = os.environ.get("ACTIONS_JSON")
if not ACTIONS_JSON_STR:
    logging.error("❌ متغیر محیطی 'ACTIONS_JSON' یافت نشد. لطفا سکرت‌ها را در گیت‌هاب تنظیم کنید.")
    exit(1)

try:
    ACTIONS = json.loads(ACTIONS_JSON_STR)
    if not isinstance(ACTIONS, list) or not ACTIONS:
        raise ValueError("لیست اکشن‌ها نامعتبر یا خالی است.")
except Exception as e:
    logging.error(f"❌ خطا در خواندن 'ACTIONS_JSON': {e}")
    exit(1)

# پنهان‌سازی پرامپت‌ها در لاگ‌های گیت‌هاب
for action in ACTIONS:
    print(f"::add-mask::{action}")

REPEAT_PER_ACTION = 10

def get_public_ip():
    try:
        with urllib.request.urlopen('https://api.ipify.org', timeout=5) as response:
            return response.read().decode('utf-8').strip()
    except Exception as e:
        return f"نامشخص (خطا: {e})"

def check_space_runtime_status(server_name, hf_token=None):
    url = f"https://huggingface.co/api/spaces/{server_name}/runtime"
    headers = {"User-Agent": "Mozilla/5.0"}
    if hf_token:
        headers["Authorization"] = f"Bearer {hf_token}"

    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=5) as response:
            data = json.loads(response.read().decode('utf-8'))
            stage = data.get("stage")
            hardware = data.get("hardware")
            logging.info(f"📊 استعلام لایو سرور {server_name}: [وضعیت: {stage}] | [سخت‌افزار: {hardware}]")
            return stage, hardware
    except Exception as e:
        logging.warning(f"⚠️ نتوانست وضعیت سرور {server_name} را از API هافینگ‌فیس استعلام کند: {e}")
        return None, None

def check_and_save_ip(current_ip):
    now = datetime.now(timezone.utc)
    active_ips = {}

    if os.path.exists(IP_FILE):
        with open(IP_FILE, 'r') as f:
            for line in f:
                line = line.strip()
                if line and ',' in line:
                    ip, ts_str = line.split(',', 1)
                    try:
                        ts = datetime.fromisoformat(ts_str)
                        if now - ts < timedelta(hours=24):
                            active_ips[ip] = ts
                    except ValueError:
                        pass

    if current_ip in active_ips:
        last_used_utc = active_ips[current_ip]
        last_used_iran = last_used_utc.astimezone(IRAN_TZ).strftime('%Y-%m-%d %H:%M:%S')
        logging.warning(f"⚠️ آی‌پی {current_ip} تکراری است! این آی‌پی قبلاً در تاریخ {last_used_iran} استفاده شده است.")
        return False

    active_ips[current_ip] = now

    with open(IP_FILE, 'w') as f:
        for ip, ts in active_ips.items():
            f.write(f"{ip},{ts.isoformat()}\n")

    try:
        subprocess.run(["git", "config", "user.name", "GitHub Action Bot"], check=True)
        subprocess.run(["git", "config", "user.email", "actions@github.com"], check=True)
        subprocess.run(["git", "add", IP_FILE], check=True)

        status = subprocess.run(["git", "status", "--porcelain"], capture_output=True, text=True)
        if status.stdout.strip():
            subprocess.run(["git", "pull", "origin", "main", "--rebase"], check=False)
            subprocess.run(["git", "commit", "-m", f"🔄 Update used IPs (Added {current_ip})"], check=True)
            subprocess.run(["git", "push"], check=True)
            logging.info("💾 لیست آی‌پی‌ها با موفقیت در مخزن گیت‌هاب ذخیره شد.")
    except Exception as e:
        logging.warning(f"⚠️ هشدار گیت: سیستم نتوانست فایل آی‌پی‌ها را در گیت ذخیره کند: {e}")

    return True

def convert_hms_to_seconds(hms_str):
    try:
        parts = list(map(int, hms_str.split(':')))
        if len(parts) == 3:
            return parts[0] * 3600 + parts[1] * 60 + parts[2]
        elif len(parts) == 2:
            return parts[0] * 60 + parts[1]
        return 0
    except Exception:
        return 0

def parse_gpu_errors(error_str):
    zg_match = re.search(
        r"exceeded your ZeroGPU quota \(([\d.-]+)s requested vs\. ([\d.-]+)s left\)\.\s*Try again in ([\d:]+)",
        error_str,
        re.IGNORECASE
    )
    if zg_match:
        requested_sec = zg_match.group(1)
        left_sec = zg_match.group(2)
        wait_time_str = zg_match.group(3)
        return {
            "type": "zerogpu_quota",
            "requested": f"{requested_sec} ثانیه",
            "left": f"{left_sec} ثانیه",
            "wait_time_str": wait_time_str,
            "wait_seconds": convert_hms_to_seconds(wait_time_str)
        }

    no_gpu_match = re.search(r"No GPU was available after (\d+)s", error_str, re.IGNORECASE)
    if no_gpu_match:
        return {
            "type": "no_gpu_available",
            "timeout": no_gpu_match.group(1),
            "wait_seconds": 60
        }

    req_match = re.search(r"Tried to allocate ([\d.]+)\s*(\w+)", error_str)
    total_match = re.search(r"([\d.]+)\s*(\w+)\s*total capacity", error_str)
    alloc_match = re.search(r"([\d.]+)\s*(\w+)\s*(?:already |)?allocated", error_str)
    free_match = re.search(r"([\d.]+)\s*(\w+)\s*free", error_str)

    if req_match or "CUDA out of memory" in error_str:
        return {
            "type": "cuda_oom",
            "requested": f"{req_match.group(1)} {req_match.group(2)}" if req_match else "نامشخص",
            "total": f"{total_match.group(1)} {total_match.group(2)}" if total_match else "نامشخص",
            "allocated": f"{alloc_match.group(1)} {alloc_match.group(2)}" if alloc_match else "نامشخص",
            "free": f"{free_match.group(1)} {free_match.group(2)}" if free_match else "نامشخص",
            "wait_seconds": 60
        }

    return None

# ================= 🚀 تابع تعمیر و بهینه‌سازی ویدیو با FFmpeg =================
def fix_video_with_ffmpeg(input_path, output_path, crf=12):
    try:
        logging.info(f"🛠️ شروع فرآیند تعمیر و فشرده‌سازی ویدیو با FFmpeg (CRF: {crf} | YUV420P)...")
        command = [
            "ffmpeg",
            "-y",
            "-i", input_path,
            "-c:v", "libx264",
            "-pix_fmt", "yuv420p",
            "-preset", "fast",
            "-crf", str(crf),
            "-c:a", "copy",
            output_path
        ]
        result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if result.returncode == 0 and os.path.exists(output_path) and os.path.getsize(output_path) > 0:
            raw_size_mb = os.path.getsize(input_path) / (1024 * 1024)
            fixed_size_mb = os.path.getsize(output_path) / (1024 * 1024)
            logging.info(f"✅ ویدیوی استاندارد با موفقیت ساخته شد! (حجم خام: {raw_size_mb:.2f}MB ⬅️ حجم نهایی: {fixed_size_mb:.2f}MB)")
            return True
        else:
            logging.error(f"❌ خطا در تبدیل FFmpeg: {result.stderr}")
            return False
    except Exception as e:
        logging.error(f"❌ استثنا در اجرای FFmpeg: {e}")
        return False

# ================= توابع گوگل درایو =================
def get_drive_service():
    client_id = os.environ.get("GDRIVE_CLIENT_ID")
    client_secret = os.environ.get("GDRIVE_CLIENT_SECRET")
    refresh_token = os.environ.get("GDRIVE_REFRESH_TOKEN")

    if not all([client_id, client_secret, refresh_token]):
        raise ValueError("اطلاعات احراز هویت گوگل درایو (Secrets) ناقص است.")

    creds = Credentials(
        token=None,
        refresh_token=refresh_token,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=client_id,
        client_secret=client_secret
    )
    return build('drive', 'v3', credentials=creds, cache_discovery=False)

def download_image_from_folder(folder_id, download_path):
    service = get_drive_service()
    query = f"'{folder_id}' in parents and trashed=false and (mimeType contains 'image/')"
    
    try:
        results = service.files().list(
            q=query,
            fields="files(id, name, mimeType)",
            pageSize=10
        ).execute()
        
        files = results.get('files', [])
        
        if not files:
            logging.error(f"❌ هیچ عکس معتبری در پوشه گوگل‌درایو با آیدی {folder_id} پیدا نشد.")
            return False, None

        image_file = files[0]
        file_id = image_file['id']
        file_name = image_file['name']
        
        logging.info(f"📥 در حال دانلود عکس '{file_name}' از گوگل درایو...")

        request = service.files().get_media(fileId=file_id)
        fh = io.FileIO(download_path, 'wb')
        downloader = MediaIoBaseDownload(fh, request)
        
        done = False
        while done is False:
            status, done = downloader.next_chunk()
            
        logging.info(f"✅ عکس '{file_name}' با موفقیت در مسیر موقت ذخیره شد.")
        return True, file_name

    except Exception as e:
        logging.error(f"❌ خطا در خواندن فایل از گوگل درایو: {e}")
        return False, None

def upload_to_gdrive(file_path, file_name, mimetype='video/mp4'):
    folder_id = os.environ.get("GDRIVE_FOLDER_ID")

    if not folder_id:
        logging.warning("⚠️ شناسه پوشه گوگل درایو (GDRIVE_FOLDER_ID) یافت نشد. آپلود لغو شد.")
        return False

    try:
        logging.info(f"☁️ در حال اتصال به گوگل درایو برای آپلود '{file_name}'...")
        service = get_drive_service()

        file_metadata = {
            'name': file_name,
            'parents': [folder_id]
        }

        media = MediaFileUpload(file_path, mimetype=mimetype, resumable=True)

        uploaded_file = service.files().create(
            body=file_metadata,
            media_body=media,
            fields='id, webViewLink'
        ).execute()

        logging.info(f"✅ فایل با موفقیت در گوگل درایو آپلود شد! لینک مشاهده: {uploaded_file.get('webViewLink')}")
        return True

    except Exception as e:
        logging.error(f"❌ خطا در آپلود فایل به گوگل درایو: {e}")
        return False

def main():
    logging.info("=== شروع عملیات جدید ===")

    current_ip = get_public_ip()
    logging.info(f"🌐 آدرس IP فعلی سیستم: {current_ip}")

    if not check_and_save_ip(current_ip):
        logging.info("🛑 پایان عملیات به دلیل تکراری بودن آی‌پی در ۲۴ ساعت گذشته.")
        return

    hf_token = os.environ.get("HF_TOKEN")
    if not hf_token:
        logging.warning("⚠️ هشدار: متغیر محیطی 'HF_TOKEN' یافت نشد. پردازش با دسترسی عمومی اجرا می‌شود.")
    else:
        logging.info("✅ توکن هافینگ‌فیس (HF_TOKEN) شناسایی شد.")

    # ================= دریافت عکس از گوگل درایو =================
    temp_image_path = os.path.join(tempfile.gettempdir(), "input_image_from_drive.jpg")
    success_download, image_name = download_image_from_folder(GDRIVE_IMAGE_FOLDER_ID, temp_image_path)
    
    if not success_download:
        logging.error("🛑 عملیات به دلیل عدم دسترسی به عکس در گوگل درایو متوقف شد.")
        return
        
    input_image_path = temp_image_path
    base_name = os.path.splitext(image_name)[0] if image_name else "drive_image"
    logging.info(f"عکس انتخاب شده برای پردازش: {image_name}")

    # ================= محاسبه Seed بر اساس تعداد ویدیوهای موفق قبلی =================
    success_count = 0
    if os.path.exists(SEED_STATE_FILE):
        with open(SEED_STATE_FILE, 'r') as f:
            try:
                success_count = int(f.read().strip())
            except ValueError:
                pass

    current_seed = success_count
    logging.info(f"🌱 تعداد ویدیوهای موفق قبلی: {success_count} | سید (Seed) این اجرا: {current_seed}")

    # ================= 🎯 انتخاب اکشن بر اساس تعداد ویدیوهای موفق =================
    action_index = success_count // REPEAT_PER_ACTION
    if action_index >= len(ACTIONS):
        logging.info(f"🎉 همه اکشن‌ها کامل شدند (کل ویدیوهای موفق: {success_count}). عملیات جدیدی وجود ندارد.")
        return

    current_action = ACTIONS[action_index]
    position_in_action = (success_count % REPEAT_PER_ACTION) + 1
    
    logging.info(
        f"🎬 اکشن این اجرا: [پرامپت مخفی در سکرت‌ها] "
        f"(اکشن {action_index + 1}/{len(ACTIONS)} | ویدیوی {position_in_action} از {REPEAT_PER_ACTION})"
    )

    video_name = f"{base_name}_seed{current_seed}_v{success_count + 1}.mp4"

    available_servers = ALL_SERVERS
    if not available_servers:
        logging.error("❌ هیچ سروری برای پردازش تعریف نشده است!")
        return

    success = False
    max_wait_retries = 10
    wait_start_time = None
    initial_gpu_status = None

    with tempfile.TemporaryDirectory() as temp_dir:
        raw_temp_video_path = os.path.join(temp_dir, f"raw_{video_name}")
        output_video_path = os.path.join(temp_dir, video_name)

        for server in available_servers:
            logging.info(f"🔄 در حال بررسی وضعیت زنده بودن سرور: {server}")

            stage, hardware = check_space_runtime_status(server, hf_token)

            if stage in ["PAUSED", "STOPPED"]:
                logging.warning(f"⚠️ سرور {server} در حالت غیرفعال/پاز شده قرار دارد و قابل استفاده نیست.")
                continue
            elif stage == "SLEEPING":
                logging.info(f"⏳ سرور {server} در حالت خواب است. اولین اتصال آن را بیدار خواهد کرد؛ ممکن است کمی طول بکشد...")
            elif stage in ["BUILDING", "RUNNING_BUILDING"]:
                logging.warning(f"⚠️ سرور {server} در حال بازسازی یا ساخت است. تلاش برای اتصال انجام می‌شود اما ممکن است خطا دهد.")

            try:
                client = Client(server, token=hf_token) if hf_token else Client(server)

                kwargs = {
                    "input_image": handle_file(input_image_path),
                    "last_image": None,
                    "prompt": f"The girl takes off her shirt. Then she {current_action}.",
                    "negative_prompt": "blurry, low quality, static, text, watermark",
                    "steps": 6,
                    "duration_seconds": 8.0,
                    "guidance_scale": 1.0,
                    "guidance_scale_2": 1.0,
                    "seed": current_seed,
                    "randomize_seed": False,
                    "quality": 10,
                    "scheduler": "UniPCMultistep",
                    "flow_shift": 3.0,
                    "frame_multiplier": 64,
                    "video_component": False,
                    "safe_mode": False,
                    "enable_safety_checker": False,
                    "api_name": "/generate_video"
                }

                retry_count = 0
                while retry_count < max_wait_retries:
                    try:
                        if wait_start_time is not None:
                            elapsed = time.time() - wait_start_time
                            logging.info(f"⏱️ زمان سپری‌شده در صف انتظار تا این لحظه: {int(elapsed // 60)} دقیقه و {int(elapsed % 60)} ثانیه")

                        result = client.predict(**kwargs)

                        video_file = result[1] if isinstance(result, (list, tuple)) and len(result) > 1 else result[0]['video']
                        shutil.copy(video_file, raw_temp_video_path)

                        logging.info(f"🎬 ویدیوی خام با موفقیت از سرور دریافت شد.")

                        fix_success = fix_video_with_ffmpeg(raw_temp_video_path, output_video_path, crf=12)

                        if not fix_success:
                            retry_count += 1
                            logging.warning(f"⚠️ ویدیوی خروجی سالم نبود (خطای FFmpeg). تلاش مجدد... (تلاش {retry_count} از {max_wait_retries})")
                            if retry_count >= max_wait_retries:
                                raise Exception("❌ ویدیوی سالم پس از حداکثر تلاش تولید نشد.")
                            time.sleep(5)
                            continue

                        upload_file_path = output_video_path

                        if wait_start_time is not None:
                            total_wait = time.time() - wait_start_time
                            logging.info(f"📊 گزارش تکمیلی آزادسازی منابع:")
                            logging.info(f"   ⏱️ کل زمان انتظار برای اختصاص سهمیه GPU: {int(total_wait // 60)} دقیقه و {int(total_wait % 60)} ثانیه")
                            if initial_gpu_status:
                                if initial_gpu_status.get("type") == "zerogpu_quota":
                                    logging.info(f"   ⚡ حجم درخواستی مدل شما: {initial_gpu_status.get('requested')}")
                                    logging.info(f"   🔴 مانده اولیه سهمیه زمانی شما: {initial_gpu_status.get('left')}")
                        else:
                            logging.info("🟢 پردازش بدون معطلی انجام شد. منابع در اولین تلاش در دسترس سیستم قرار گرفتند.")

                        upload_success = upload_to_gdrive(upload_file_path, video_name, mimetype='video/mp4')

                        if not upload_success:
                            retry_count += 1
                            logging.warning(f"⚠️ آپلود به گوگل درایو ناموفق بود. تلاش مجدد... (تلاش {retry_count} از {max_wait_retries})")
                            if retry_count >= max_wait_retries:
                                raise Exception("❌ آپلود پس از حداکثر تلاش انجام نشد.")
                            time.sleep(5)
                            continue

                        success_count += 1
                        with open(SEED_STATE_FILE, 'w') as f:
                            f.write(str(success_count))

                        try:
                            subprocess.run(["git", "add", SEED_STATE_FILE], check=True)
                            status = subprocess.run(["git", "status", "--porcelain"], capture_output=True, text=True)
                            if status.stdout.strip():
                                subprocess.run(
                                    ["git", "commit", "-m", f"✅ Action {action_index + 1} (Hidden Prompt) | Video #{success_count}"],
                                    check=True
                                )
                                subprocess.run(["git", "pull", "origin", "main", "--rebase"], check=False)
                                subprocess.run(["git", "push"], check=True)
                                logging.info(f"💾 وضعیت با موفقیت ذخیره شد. تعداد کل ویدیوهای موفق: {success_count}")
                        except Exception as e:
                            logging.warning(f"⚠️ هشدار گیت: سیستم نتوانست فایل وضعیت سید را ذخیره کند: {e}")

                        logging.info(f"🗑️ ویدیوها در فضای موقت قرار داشتند و به صورت خودکار پاک‌سازی شدند.")

                        success = True
                        break

                    except Exception as ve:
                        error_str = str(ve)

                        match_param = re.search(r"Parameter `(.*?)` is not a valid key-word argument", error_str)
                        if match_param:
                            bad_param = match_param.group(1)
                            if bad_param in kwargs:
                                del kwargs[bad_param]
                                logging.info(f"⚙️ پارامتر ناسازگار '{bad_param}' حذف شد. تلاش مجدد...")
                                continue

                        parsed_error = parse_gpu_errors(error_str)

                        if parsed_error:
                            if wait_start_time is None:
                                wait_start_time = time.time()
                                initial_gpu_status = parsed_error

                            retry_count += 1

                            if parsed_error["type"] == "zerogpu_quota":
                                logging.warning(f"⚠️ سهمیه استفاده از پردازش گرافیکی (ZeroGPU Quota) تمام شده است.")
                                logging.warning(f"   - درخواست فرآیند: {parsed_error['requested']}")
                                logging.warning(f"   - سهمیه فعلی باقی‌مانده شما: {parsed_error['left']}")
                                logging.warning(f"   - زمان انتظار اعلام شده توسط سرور: {parsed_error['wait_time_str']}")

                                wait_seconds = parsed_error["wait_seconds"]

                                if wait_seconds == 0:
                                    wait_seconds = 30

                                if wait_seconds > MAX_WAIT_LIMIT_SECONDS:
                                    logging.warning(f"🛑 زمان انتظار مورد نیاز ({parsed_error['wait_time_str']}) از حد مجاز بیشتر است. عملیات متوقف می‌شود.")
                                    raise ve

                                logging.info(f"⏳ سیستم به مدت {wait_seconds} ثانیه وارد حالت انتظار می‌شود... (تلاش {retry_count} از {max_wait_retries})")
                                time.sleep(wait_seconds)
                                continue

                            elif parsed_error["type"] == "no_gpu_available":
                                logging.warning(f"⚠️ صف سرور شلوغ است و کارت گرافیک آزاد نیست (Timeout {parsed_error['timeout']}s).")
                                logging.info(f"⏳ سیستم به مدت {parsed_error['wait_seconds']} ثانیه صبر می‌کند... (تلاش {retry_count} از {max_wait_retries})")
                                time.sleep(parsed_error["wait_seconds"])
                                continue

                            elif parsed_error["type"] == "cuda_oom":
                                logging.warning(f"⚠️ خطای کمبود حافظه سخت‌افزاری کارت گرافیک (CUDA OOM).")
                                logging.warning(f"   - ظرفیت کل: {parsed_error['total']} | مشغول: {parsed_error['allocated']} | آزاد: {parsed_error['free']}")
                                logging.info(f"⏳ سیستم به مدت {parsed_error['wait_seconds']} ثانیه صبر می‌کند... (تلاش {retry_count} از {max_wait_retries})")
                                time.sleep(parsed_error["wait_seconds"])
                                continue

                        raise ve

                if success:
                    break

            except Exception as e:
                error_msg = str(e)
                logging.error(f"❌ خطا در سرور {server}: {error_msg}")

                if "guardrails" in error_msg.lower() or "sensitive" in error_msg.lower():
                    logging.warning(f"⚠️ سرور {server} درخواست را به دلیل فیلتر ایمنی (Guardrails) رد کرد.")
                elif "quota" in error_msg.lower() and "exceeded" in error_msg.lower():
                    logging.warning(f"⚠️ ظرفیت یا سهمیه سرور به اتمام رسیده است.")
                elif "aborted" in error_msg.lower():
                    logging.warning(f"⚠️ فرآیند در سرور لغو شد.")

        if not success:
            logging.error("❌ عملیات روی تمام سرورها با شکست مواجه شد.")
            try:
                failure_txt_name = f"failed_{base_name}_seed{current_seed}.txt"
                failure_txt_path = os.path.join(temp_dir, failure_txt_name)
                iran_time_str = datetime.now(IRAN_TZ).strftime('%Y-%m-%d %H:%M:%S')

                with open(failure_txt_path, 'w', encoding='utf-8') as f:
                    f.write("🔴 خطا در پردازش تصویر\n")
                    f.write(f"تصویر مبدا: {image_name}\n")
                    f.write(f"اکشن: [پرامپت مخفی]\n") 
                    f.write(f"سید استفاده شده: {current_seed}\n")
                    f.write(f"زمان تلاش سیستم (به وقت ایران): {iran_time_str}\n")
                    f.write("توضیحات: تلاش برای ساخت ویدیو روی تمامی سرورهای تعریف شده با خطا مواجه شد و فرآیند ناموفق بود.\n")

                logging.info(f"📄 فایل گزارش عدم موفقیت '{failure_txt_name}' به صورت محلی ایجاد شد.")
                upload_to_gdrive(failure_txt_path, failure_txt_name, mimetype='text/plain')

            except Exception as file_err:
                logging.error(f"❌ خطا در ایجاد یا آپلود فایل گزارش خطا: {file_err}")

    # پاک‌سازی فایل عکس دانلود شده از گوگل درایو
    if os.path.exists(input_image_path):
        os.remove(input_image_path)
        logging.info("🗑️ فایل عکس موقت پس از پردازش حذف شد.")

if __name__ == "__main__":
    main()
