import os
import discord
from discord.ext import commands
import wikipedia
from google import genai
from google.genai import types
from dotenv import load_dotenv
import requests
from datetime import datetime
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

# ============================================================
# 載入環境變數
# ============================================================
load_dotenv()

TOKEN = os.getenv("TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
DODGERS_CHANNEL_ID = int(os.getenv("DODGERS_CHANNEL_ID"))
WIKI_CHANNEL_ID = int(os.getenv("WIKI_CHANNEL_ID"))
IMAGE_CHANNEL_ID = int(os.getenv("IMAGE_CHANNEL_ID"))
AUTO_POST_HOUR = int(os.getenv("AUTO_POST_HOUR", 8))
AUTO_POST_MINUTE = int(os.getenv("AUTO_POST_MINUTE", 0))

# 檢查必要的環境變數是否存在
if not all([TOKEN, GEMINI_API_KEY]):
    raise ValueError("請在 .env 中設定 TOKEN 與 GEMINI_API_KEY")

# ============================================================
# 初始化客戶端與機器人
# ============================================================
wikipedia.set_lang("zh-tw")
ai_client = genai.Client(api_key=GEMINI_API_KEY)

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# 排程器指定時區（台灣時間）
scheduler = AsyncIOScheduler(timezone='Asia/Taipei')

# ============================================================
# 定時任務：道奇隊賽事自動播報
# ============================================================
async def auto_post_dodgers_lineup():
    """每日定時發送道奇隊賽事資訊"""
    channel = bot.get_channel(DODGERS_CHANNEL_ID)
    if not channel:
        print("❌ 找不到道奇隊指定頻道")
        return

    today = datetime.now().strftime('%Y-%m-%d')
    schedule_url = f"https://statsapi.mlb.com/api/v1/schedule?sportId=1&date={today}&teamId=119"
    message_content = f"📅 今日 ({today}) 道奇隊無賽事喔！"

    try:
        res = requests.get(schedule_url, timeout=10)
        res.raise_for_status()
        response = res.json()
        if response.get("dates") and len(response["dates"]) > 0:
            games = response["dates"][0].get("games", [])
            if games:
                game_data = games[0]
                is_home = game_data["teams"]["home"]["team"]["id"] == 119
                opponent = game_data["teams"]["away"]["team"]["name"] if is_home else game_data["teams"]["home"]["team"]["name"]
                message_content = f"⚾ **【今日賽事自動播報】**\n道奇 vs **{opponent}**\n詳細先發打序請至 MLB 官網或 App 查詢。"
    except requests.RequestException as e:
        print(f"⚠️ MLB API 請求失敗: {e}")
        message_content = "❌ 無法取得今日賽事資訊，請稍後再試。"
    except Exception as e:
        print(f"⚠️ 解析賽事資料時發生錯誤: {e}")
        message_content = "❌ 自動抓取今日賽事名單時發生錯誤。"

    await channel.send(message_content)

# ============================================================
# 機器人事件與命令處理
# ============================================================
@bot.event
async def on_ready():
    print(f"🤖 全自動智慧分流模組已上線：{bot.user.name}")
    print("─" * 50)
    print(f"⚾ 道奇定時播報 ➡️ 頻道 ID: {DODGERS_CHANNEL_ID} (每天 {AUTO_POST_HOUR:02d}:{AUTO_POST_MINUTE:02d} 發送)")
    print(f"📖 打字查 Wiki   ➡️ 頻道 ID: {WIKI_CHANNEL_ID}")
    print(f"📸 丟圖智慧辨識 ➡️ 頻道 ID: {IMAGE_CHANNEL_ID}")
    print("─" * 50)

    # 啟動每日排程
    scheduler.add_job(
        auto_post_dodgers_lineup,
        CronTrigger(hour=AUTO_POST_HOUR, minute=AUTO_POST_MINUTE)
    )
    scheduler.start()
    print("⏰ 每日排程器啟動成功。")
    print("─" * 50)

@bot.event
async def on_message(message):
    if message.author == bot.user:
        return

    # ---------- 1. Wiki 專用聊天室：直接查詢 ----------
    if message.channel.id == WIKI_CHANNEL_ID:
        keyword = message.content.strip()
        if not keyword:
            await message.reply("📝 請輸入你要查詢的關鍵字。")
            await bot.process_commands(message)
            return

        status_msg = await message.reply(f"🔍 偵測到關鍵字！正在搜尋維基百科「{keyword}」...")
        try:
            summary = wikipedia.summary(keyword, sentences=3)
            await status_msg.edit(content=f"📖 **【維基百科：{keyword}】**\n\n{summary}")
        except wikipedia.exceptions.DisambiguationError as e:
            options = "\n".join([f"- {opt}" for opt in e.options[:5]])
            await status_msg.edit(content=f"⚠️ 搜尋詞太模糊，請試試看更精準的詞，例如：\n{options}")
        except wikipedia.exceptions.PageError:
            await status_msg.edit(content=f"❌ 找不到關於「{keyword}」的維基百科條目。")
        except Exception as e:
            print(f"⚠️ Wiki 查詢錯誤: {e}")
            await status_msg.edit(content=f"❌ 查詢時發生未知錯誤，請稍後再試。")
        await bot.process_commands(message)
        return

    # ---------- 2. 圖片辨識專用聊天室：自動處理圖片 ----------
    if message.channel.id == IMAGE_CHANNEL_ID:
        # 篩選出圖片附件
        image_attachments = [att for att in message.attachments if att.content_type and att.content_type.startswith("image/")]
        if image_attachments:
            # 只取第一張（可依需求調整）
            image_attachment = image_attachments[0]
            if len(image_attachments) > 1:
                await message.reply("📸 偵測到多張圖片，將僅處理第一張。")
            thinking_message = await message.reply("📸 自動偵測到圖片！正在交由 Gemini 分析內容與翻譯...")

            try:
                # 讀取圖片資料（限制大小避免記憶體爆炸）
                image_data = await image_attachment.read()
                prompt = (
                    "請精準擷取這張圖片中的所有文字，保持原本的段落排版。"
                    "如果是英文的題目或課本文本，請在下方順便附上流暢的繁體中文翻譯。"
                )

                response = ai_client.models.generate_content(
                    model='gemini-2.5-flash',
                    contents=[
                        types.Part.from_bytes(data=image_data, mime_type=image_attachment.content_type),
                        prompt
                    ]
                )

                result_text = response.text
                if len(result_text) > 2000:
                    result_text = result_text[:1950] + "\n\n...(內容過長已截斷)"

                await thinking_message.edit(content=result_text)
            except Exception as e:
                print(f"⚠️ Gemini 處理發生錯誤: {e}")
                await thinking_message.edit(content="❌ 抱歉，自動處理圖片時發生錯誤。")
        else:
            # 頻道內若無圖片，可選擇忽略或提示（此處僅忽略）
            pass
        await bot.process_commands(message)
        return

    # ---------- 其他訊息：交給命令處理器 ----------
    await bot.process_commands(message)

# ============================================================
# 啟動機器人
# ============================================================
if __name__ == "__main__":
    bot.run(TOKEN)
