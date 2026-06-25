import datetime
import io
import os
import time  # 👈 新增：用於計算時間差
import requests
import wikipedia
import discord
from discord.ext import commands
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from google import genai
from google.genai import types

# ==================== 🛠️ 請在這裡修改你的設定 ====================
TOKEN = "MTUxOTYzMDEwMjUwODQ3MDI3NA.GrzinC.Ipvno_M-PVaw_e3IgCf2NmW6AvDzPF5LRdrbkA"
GEMINI_API_KEY = "AQ.Ab8RN6I6LYWBOmXEyPloggUCIZonUuH8KB-TBKQqC0bhyE3D_w"

REMIND_CHANNEL_ID = 1519642817637646438  # 每日道奇名單推播的頻道 ID
SEARCH_CHANNEL_ID = 1519663745192825024  # 專屬搜尋頻道的 ID
# ================================================================

# 初始化 Discord 機器人設定
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# 初始化 Google Gemini AI 客戶端
ai_client = genai.Client(api_key=GEMINI_API_KEY)

# 其它套件設定
DODGERS_TEAM_ID = 119
wikipedia.set_lang("zh")

# ⏱️ 紀錄使用者最後觸發功能的時間 (格式: {user_id: timestamp})
last_image_time = {}
last_wiki_time = {}

# 調整冷卻時間（秒）
IMAGE_COOLDOWN = 3  # 圖片辨識每 30 秒只能用一次
WIKI_COOLDOWN = 3   # 維基搜尋每 15 秒只能用一次


# ==============================================================================
#  功能區塊一：【道奇隊名單功能】(MLB API)
# ==============================================================================
def get_position_name(code):
    pos_map = {
        "1": "投手 P", "2": "捕手 C", "3": "一壘手 1B", "4": "二壘手 2B",
        "5": "三壘手 3B", "6": "游擊手 SS", "7": "左外野手 LF", "8": "中外野手 CF",
        "9": "右外野手 RF", "DH": "指定打擊 DH",
    }
    return pos_map.get(code, code)


def fetch_dodgers_lineup():
    today = datetime.date.today().strftime("%Y-%m-%d")
    schedule_url = f"https://statsapi.mlb.com/api/v1/schedule?sportId=1&date={today}&teamId={DODGERS_TEAM_ID}"
    try:
        response = requests.get(schedule_url).json()
        if not response.get("dates") or len(response["dates"][0]["games"]) == 0:
            return discord.Embed(
                title="📅 今日無賽事",
                description=f"道奇隊今天 ({today}) 沒有比賽喔！",
                color=0x888888,
            )

        game_data = response["dates"][0]["games"][0]
        game_pk = game_data["gamePk"]
        is_home = game_data["teams"]["home"]["team"]["id"] == DODGERS_TEAM_ID
        opponent = game_data["teams"]["away"]["team"]["name"] if is_home else game_data["teams"]["home"]["team"]["name"]

        boxscore_url = f"https://statsapi.mlb.com/api/v1/game/{game_pk}/boxscore"
        boxscore_res = requests.get(boxscore_url).json()

        team_key = "home" if is_home else "away"
        dodgers_data = boxscore_res["teams"][team_key]
        lineup_ids = dodgers_data.get("battingOrder", [])

        if not lineup_ids:
            return discord.Embed(
                title=f"⚾ 今日賽事：道奇 vs {opponent}",
                description=f"日期：{today}\n\n⚠️ **官方尚未公布今日的先發出賽名單！**\n（通常於開賽前 2~3 小時公布）",
                color=0xFFCC00,
            )

        embed = discord.Embed(
            title="💙 道奇隊今日先發出賽名單 💙",
            description=f"對手：**{opponent}**\n日期：{today}",
            color=0x005A9C,
        )

        lineup_text = ""
        for index, player_id in enumerate(lineup_ids, 1):
            player_key = f"ID{player_id}"
            player = dodgers_data["players"].get(player_key, {})
            if player:
                name = player["person"]["fullName"]
                pos = player["position"]["abbreviation"]
                pos_zh = get_position_name(pos)
                jersey = player["jerseyNumber"]
                lineup_text += f"**{index}棒** | #{jersey} {name} ({pos_zh})\n"

        pitcher_id = dodgers_data.get("pitchers", [None])[0]
        if pitcher_id:
            pitcher_player = dodgers_data["players"].get(f"ID{pitcher_id}", {})
            p_name = pitcher_player["person"]["fullName"]
            p_jersey = pitcher_player["jerseyNumber"]
            embed.add_field(name="🔥 今日先發投手", value=f"#{p_jersey} {p_name}", inline=False)

        embed.add_field(name="📋 先發打序", value=lineup_text, inline=False)
        embed.set_thumbnail(url="https://midfield.com/wp-content/uploads/2021/04/LA-Logo.png")
        return embed
    except Exception as e:
        print(f"抓取名單發生錯誤: {e}")
        return discord.Embed(title="❌ 錯誤", description="抓取名單時發生未知錯誤。", color=0xFF0000)


async def auto_send_lineup():
    channel = bot.get_channel(REMIND_CHANNEL_ID)
    if channel:
        embed_message = fetch_dodgers_lineup()
        await channel.send(embed=embed_message)


# ==============================================================================
#  核心事件監聽 (處理開機、訊息、搜尋頻道、AI 圖片識別)
# ==============================================================================
@bot.event
async def on_ready():
    print(f"✅ {bot.user} 已成功上線！")
    scheduler = AsyncIOScheduler()
    scheduler.add_job(auto_send_lineup, "cron", hour=8, minute=0)
    scheduler.start()
    print("⏰ 每日道奇隊名單排程已啟動！(每日早上 08:00 推播)")


@bot.event
async def on_message(message):
    if message.author == bot.user:
        return

    # ✨ 【功能二：Gemini AI 圖片文字擷取與分析（含防洗版限制）】
    if message.attachments:
        for attachment in message.attachments:
            if attachment.filename.lower().endswith((".png", ".jpg", ".jpeg", ".webp")):
                user_id = message.author.id
                current_time = time.time()

                # 檢查冷卻時間
                if user_id in last_image_time and current_time - last_image_time[user_id] < IMAGE_COOLDOWN:
                    remaining = int(IMAGE_COOLDOWN - (current_time - last_image_time[user_id]))
                    await message.reply(f"🛑 系統冷卻中！AI 辨識圖片功能很耗資源，請等待 {remaining} 秒後再試。")
                    return

                # 通過檢查，更新時間並執行
                last_image_time[user_id] = current_time
                await message.channel.send("🤖 **AI 正在辨識並分析圖片中的文字，請稍候...**")

                try:
                    image_bytes = await attachment.read()
                    mime_type = attachment.content_type or "image/jpeg"

                    response = ai_client.models.generate_content(
                        model="gemini-2.5-flash",
                        contents=[
                            types.Part.from_bytes(data=image_bytes, mime_type=mime_type),
                            "請精準擷取這張圖片中的所有文字，保持原本的段落排版。如果是英文的題目或課本文本，請在下方順便附上流暢的繁體中文翻譯。",
                        ],
                    )

                    if response.text:
                        result = response.text
                        if len(result) > 1900:
                            await message.channel.send(f"📝 **AI 擷取與分析結果 (上) :**\n{result[:1900]}")
                            await message.channel.send(f"{result[1900:]}")
                        else:
                            await message.channel.send(f"📝 **AI 擷取與分析結果 :**\n{result}")
                    else:
                        await message.channel.send("❓ AI 辨識完成，但未發現明顯文字。")

                except Exception as e:
                    print(f"❌ Gemini AI 發生錯誤: {e}")
                    await message.channel.send("❌ AI 服務暫時無法回應，請稍後再試。")
                return

    # ✨ 【功能三：專屬頻道 Wikipedia 搜尋（含防洗版限制）】
    if message.channel.id == SEARCH_CHANNEL_ID:
        if message.content.startswith(("! ", "!")):
            await bot.process_commands(message)
            return

        keyword = message.content.strip()
        if not keyword:
            return

        user_id = message.author.id
        current_time = time.time()

        # 檢查冷卻時間
        if user_id in last_wiki_time and current_time - last_wiki_time[user_id] < WIKI_COOLDOWN:
            remaining = int(WIKI_COOLDOWN - (current_time - last_wiki_time[user_id]))
            await message.reply(f"🛑 搜尋太頻繁囉！請等待 {remaining} 秒後再查詢。")
            return

        # 通過檢查，更新時間並執行
        last_wiki_time[user_id] = current_time
        await message.channel.send(f"🔍 正在維基百科搜尋「{keyword}」...")
        try:
            summary = wikipedia.summary(keyword, sentences=3)
            page = wikipedia.page(keyword)
            url = page.url
            embed = discord.Embed(title=f"📝 結果：{keyword}", description=summary, color=0x2ECC71)
            embed.add_field(name="🔗 連結", value=url, inline=False)
            await message.channel.send(embed=embed)
        except Exception as e:
            print(f"維基搜尋錯誤: {e}")
            await message.channel.send(f"❌ 找不到關於「{keyword}」的資料，請換個詞試試看！")
        return

    await bot.process_commands(message)


# ==============================================================================
#  功能區塊四：【常規文字指令】（使用內建 Cooldown）
# ==============================================================================
@bot.command()
async def hello(ctx):
    """打招呼指令"""
    await ctx.send(f"你好，{ctx.author.mention}！")


# 👈 新增限制：每 10 秒內，同一個使用者（BucketType.user）只能執行 1 次
@bot.command(name="道奇名單")
@commands.cooldown(1, 10, commands.BucketType.user)
async def dodgers_lineup(ctx):
    """手動查詢道奇名單指令"""
    await ctx.send("正在幫你從 MLB 官網抓取最新道奇隊出賽名單...")
    embed_message = fetch_dodgers_lineup()
    await ctx.send(embed=embed_message)


# 👈 新增：當指令觸發冷卻限制時，會自動捕捉並發送警告訊息，而不會在後台報錯
@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandOnCooldown):
        await ctx.reply(f"🛑 此指令正在冷卻中！請等待 {int(error.retry_after)} 秒後再試。")
    else:
        raise error


# 啟動機器人
bot.run(TOKEN)