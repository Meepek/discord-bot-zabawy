import discord
from discord.ext import commands, tasks
import os
import google.generativeai as genai
import re
import random
import json
import psycopg2
import psycopg2.extras
import time
import asyncio
import google.api_core.exceptions
from discord import app_commands, ui
from typing import Literal
from google.generativeai.types import HarmCategory, HarmBlockThreshold

# --- KONFIGURACJA ---
DISCORD_TOKEN = os.getenv('DISCORD_TOKEN')
GOOGLE_API_KEY = os.getenv('GOOGLE_API_KEY')
DATABASE_URL = os.getenv('DATABASE_URL')
LOG_CHANNEL_ID = 123456789012345678 # <<<================ ZASTĄP PRAWDZIWYM ID KANAŁU LOGÓW

if not all([DISCORD_TOKEN, GOOGLE_API_KEY, DATABASE_URL]):
    print("BŁĄD: Brak kluczowych zmiennych środowiskowych (TOKEN, API_KEY, DATABASE_URL).")
    exit()

genai.configure(api_key=GOOGLE_API_KEY)
model = genai.GenerativeModel('gemini-flash-latest')

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)

player_games, channel_wide_games = {}, {}
IDLE_TIMEOUT = 90
POINTS = {"łatwy": 10, "normalny": 15, "trudny": 25}
ACHIEVEMENTS = {
    "FIRST_WIN": {"name": "Pierwsze Kroki", "description": "Wygraj swoją pierwszą grę!", "points": 10},
    "WORDLE_PRO": {"name": "Słowny Geniusz", "description": "Odgadnij słowo w Wordle w 2 próbach.", "points": 50},
    "QUIZ_MASTER": {"name": "Mózg Operacji", "description": "Wygraj 5 gier w Quiz.", "points": 25},
    "DEDECTIVE": {"name": "Mistrz Dedukcji", "description": "Wygraj w 'Zgadnij Co' w mniej niż 10 pytaniach.", "points": 30},
    "SOCIALITE": {"name": "Dusza Towarzystwa", "description": "Wygraj grę w Tabu.", "points": 20},
    "SCRIBE": {"name": "Pisarz", "description": "Dopisz 5 zdań w grze 'Historia'.", "points": 15},
}

# --- FUNKCJE BAZY DANYCH (POSTGRESQL) ---
def get_db_connection(): return psycopg2.connect(DATABASE_URL)

def setup_database():
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""CREATE TABLE IF NOT EXISTS users (user_id BIGINT PRIMARY KEY, user_name TEXT, score INT DEFAULT 0, quiz_wins INT DEFAULT 0, wordle_wins INT DEFAULT 0, story_posts INT DEFAULT 0)""")
            cur.execute("""CREATE TABLE IF NOT EXISTS achievements (user_id BIGINT, achievement_id TEXT, PRIMARY KEY (user_id, achievement_id))""")
            cur.execute("""CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)""")
            cur.execute("INSERT INTO settings (key, value) VALUES ('maintenance_mode', 'false') ON CONFLICT (key) DO NOTHING")
        conn.commit()
    print("Baza danych PostgreSQL gotowa.")

def update_user_score(user_id, user_name, points=0, **kwargs):
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO users (user_id, user_name) VALUES (%s, %s) ON CONFLICT (user_id) DO UPDATE SET user_name = EXCLUDED.user_name", (user_id, str(user_name)))
            parts, params = ["score = score + %s"], [points]
            if kwargs.get('quiz_win'): parts.append("quiz_wins = quiz_wins + 1")
            if kwargs.get('wordle_win'): parts.append("wordle_wins = wordle_wins + 1")
            if kwargs.get('story_post'): parts.append("story_posts = story_posts + 1")
            query = f"UPDATE users SET {', '.join(parts)} WHERE user_id = %s"
            params.append(user_id); cur.execute(query, tuple(params))
        conn.commit()

def grant_achievement(user_id, ach_id):
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM achievements WHERE user_id = %s AND achievement_id = %s", (user_id, ach_id))
            if cur.fetchone() is None:
                cur.execute("INSERT INTO achievements (user_id, achievement_id) VALUES (%s, %s)", (user_id, ach_id)); conn.commit(); return True
    return False

def get_user_stats(user_id):
    with get_db_connection() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur: cur.execute("SELECT * FROM users WHERE user_id = %s", (user_id,)); return cur.fetchone()
def get_user_achievements(user_id):
    with get_db_connection() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur: cur.execute("SELECT achievement_id FROM achievements WHERE user_id = %s", (user_id,)); return cur.fetchall()
def get_leaderboard(limit=10):
    with get_db_connection() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur: cur.execute("SELECT user_name, score FROM users ORDER BY score DESC LIMIT %s", (limit,)); return cur.fetchall()
def get_allowed_channels():
    with get_db_connection() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur: cur.execute("SELECT value FROM settings WHERE key = 'allowed_channels'"); row = cur.fetchone(); return json.loads(row['value']) if row else []
def set_allowed_channels(cids):
    with get_db_connection() as conn:
        with conn.cursor() as cur: cur.execute("INSERT INTO settings (key, value) VALUES ('allowed_channels', %s) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value", (json.dumps(list(set(cids))),)); conn.commit()
def get_setting(key):
    with get_db_connection() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur: cur.execute("SELECT value FROM settings WHERE key = %s", (key,)); return cur.fetchone()
def set_setting(key, value):
    with get_db_connection() as conn:
        with conn.cursor() as cur: cur.execute("INSERT INTO settings (key, value) VALUES (%s, %s) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value", (key, str(value))); conn.commit()

# --- FUNKCJE POMOCNICZE ---
async def post_log(level, title, description="", fields=None, ctx=None):
    if LOG_CHANNEL_ID == 123456789012345678: return
    log_channel = bot.get_channel(LOG_CHANNEL_ID)
    if not log_channel: print(f"BŁĄD: Nie mogę znaleźć kanału logów {LOG_CHANNEL_ID}."); return

    emojis = {"INFO": "ℹ️", "SUCCESS": "✅", "FAIL": "❌", "ERROR": "🚨", "WARNING": "⚠️"}
    colors = {"INFO": 0x3498db, "SUCCESS": 0x2ecc71, "FAIL": 0xe67e22, "ERROR": 0xe74c3c, "WARNING": 0xf1c40f}
    
    embed = discord.Embed(title=f"{emojis.get(level, '❓')} {title}", description=description, color=colors.get(level, 0x99aab5), timestamp=discord.utils.utcnow())
    if ctx:
        user = None
        if isinstance(ctx, discord.Interaction): user = ctx.user
        elif isinstance(ctx, discord.Message): user = ctx.author
        elif isinstance(ctx, (discord.Member, discord.User)): user = ctx
        if user: embed.set_author(name=user, icon_url=user.display_avatar.url)
    if fields:
        for name, value in fields.items(): embed.add_field(name=name, value=str(value) or "Brak", inline=False)
    
    try: await log_channel.send(embed=embed)
    except Exception as e: print(f"Błąd wysyłania logu: {e}")

async def check_and_grant_achievements(user, channel, **kwargs):
    user_stats = get_user_stats(user.id)
    if not user_stats: return

    async def announce_achievement(ach_id):
        ach = ACHIEVEMENTS[ach_id]
        update_user_score(user.id, user.name, points=ach["points"])
        await channel.send(f"🏆 {user.mention} odblokował osiągnięcie: **{ach['name']}**! (+{ach['points']} pkt)")
        await post_log("INFO", f"🏅 Zdobyto Osiągnięcie", description=f"Gracz {user.mention} zdobył **{ach['name']}**.", ctx=user)

    total_wins = user_stats['quiz_wins'] + user_stats['wordle_wins']
    if total_wins >= 1 and grant_achievement(user.id, "FIRST_WIN"): await announce_achievement("FIRST_WIN")
    if kwargs.get('wordle_attempts') == 2 and grant_achievement(user.id, "WORDLE_PRO"): await announce_achievement("WORDLE_PRO")
    if user_stats['quiz_wins'] >= 5 and grant_achievement(user.id, "QUIZ_MASTER"): await announce_achievement("QUIZ_MASTER")
    if kwargs.get('20q_win') and kwargs.get('questions_asked', 21) <= 10 and grant_achievement(user.id, "DEDECTIVE"): await announce_achievement("DEDECTIVE")
    if kwargs.get('taboo_win') and grant_achievement(user.id, "SOCIALITE"): await announce_achievement("SOCIALITE")
    if user_stats['story_posts'] >= 5 and grant_achievement(user.id, "SCRIBE"): await announce_achievement("SCRIBE")

async def generate_from_ai(prompt, is_json=False, temp=0.9):
    safety_settings = {cat: HarmBlockThreshold.BLOCK_NONE for cat in HarmCategory}
    try:
        response = await model.generate_content_async(prompt, generation_config=genai.GenerationConfig(temperature=temp), safety_settings=safety_settings)
        text = response.text.strip()
        if is_json: return json.loads(re.sub(r'```json\s*|\s*```', '', text, flags=re.DOTALL))
        return text
    except google.api_core.exceptions.ResourceExhausted:
        await post_log("WARNING", "Przekroczono limit API Google", description="Zbyt wiele zapytań. Czekam 60 sekund."); print("Przekroczono limit API, czekam 60s...")
        await asyncio.sleep(60); return await generate_from_ai(prompt, is_json, temp)
    except Exception as e:
        if "response.candidates' is empty" in str(e): await post_log("WARNING", "Odpowiedź AI zablokowana", description="Filtry bezpieczeństwa Google zablokowały odpowiedź.", fields={"Prompt": f"```{prompt[:1000]}...```"})
        else: await post_log("ERROR", "Błąd API Google AI", description=f"```\n{e}\n```")
        return None

  async def generate_word(length, difficulty):
    diff_prompt = {"łatwy": "popularne", "normalny": "powszechne", "trudny": "rzadkie"}
    prompt = f"Podaj jedno, {diff_prompt[difficulty]} polskie słowo (rzeczownik), {length} liter, bez polskich znaków. TYLKO SŁOWO."
    word = await generate_from_ai(prompt, temp=1.0)
    if word and len(word) == length and re.match(f"^[A-Z]{{{length}}}$", word): return word
    else: return await generate_word(length, difficulty)

async def generate_quiz_question(category, difficulty):
    prompt = f'Stwórz {difficulty} pytanie quizowe z kategorii "{category}". Losowo przypisz poprawną odpowiedź do A, B, C lub D. Jeśli kategoria jest dziwna, wymyśl kreatywne pytanie. JSON: {{"question": "...", "answers": {{"A": "...", "B": "...", "C": "...", "D": "..."}}, "correct_answer": "A"}}'
    return await generate_from_ai(prompt, is_json=True)

async def answer_yes_no(question, secret_object, history):
    hist_text = "\n".join([f"P: {h['q']} | O: {h['a']}" for h in history])
    prompt = f'Gra w 20 pytań. Sekretny obiekt: "{secret_object}". Historia:\n{hist_text}\n\nPytanie: "{question}"\n\nOdpowiedz krótko: TAK, NIE, CZASAMI, RACZEJ TAK, RACZEJ NIE, NIEISTOTNE.'
    return await generate_from_ai(prompt)

async def generate_hint(secret_object):
    return await generate_from_ai(f'Podaj krótką podpowiedź o "{secret_object}", nie zdradzając go.')

async def set_channels_lock(lock_status, guild, interaction):
    cids = get_allowed_channels() or [interaction.channel_id]
    perms = discord.PermissionOverwrite(send_messages=not lock_status)
    for cid in cids:
        if ch := bot.get_channel(cid):
            try: await ch.set_permissions(guild.default_role, overwrite=perms)
            except discord.Forbidden: await post_log("ERROR", "Błąd Blokady", description=f"Nie mam uprawnień do zarządzania kanałem {ch.mention}.")

def check_wordle_guess(guess, secret):
    fb, s_letters, g_letters = ['⬛']*len(secret), list(secret), list(guess)
    for i in range(len(secret)):
        if g_letters[i] == s_letters[i]: fb[i], s_letters[i], g_letters[i] = '🟩', None, None
    for i in range(len(secret)):
        if g_letters[i] and g_letters[i] in s_letters: fb[i] = '🟨'; s_letters[s_letters.index(g_letters[i])] = None
    return "".join(fb)

def display_hangman(game):
    art = ["  +---+\n  |   |\n      |\n      |\n      |\n      |\n===", "  +---+\n  |   |\n  O   |\n      |\n      |\n      |\n===", "  +---+\n  |   |\n  O   |\n  |   |\n      |\n      |\n===", "  +---+\n  |   |\n  O   |\n /|   |\n      |\n      |\n===", "  +---+\n  |   |\n  O   |\n /|\\  |\n      |\n      |\n===", "  +---+\n  |   |\n  O   |\n /|\\  |\n /    |\n      |\n===", "  +---+\n  |   |\n  O   |\n /|\\  |\n / \\  |\n      |\n==="]
    word = " ".join([l if l in game['guessed_letters'] else "_" for l in game['word']])
    msg = f"```\n{art[min(game['wrong_guesses'], 6)]}\n```\n**Słowo:** `{word}`\n"
    if game.get('guessed_letters'): msg += f"**Użyte:** {', '.join(sorted(game.get('guessed_letters', [])))}\n"
    msg += f"**Błędy:** {game['wrong_guesses']}/{game['max_wrong_guesses']}"
    return msg

class ConfirmResetView(ui.View):
    def __init__(self, author_id): super().__init__(timeout=60); self.author_id, self.confirmed = author_id, None
    async def interaction_check(self, i: discord.Interaction):
        if i.user.id != self.author_id: await i.response.send_message("Tylko inicjator.", ephemeral=True); return False
        return True
    @ui.button(label="Tak, zresetuj!", style=discord.ButtonStyle.danger)
    async def confirm(self, i, b): self.confirmed=True; self.stop(); [item.disable() for item in self.children]; await i.response.edit_message(content="✅ **Resetuję...**", view=self)
    @ui.button(label="Anuluj", style=discord.ButtonStyle.secondary)
    async def cancel(self, i, b): self.confirmed=False; self.stop(); [item.disable() for item in self.children]; await i.response.edit_message(content="👍 **Anulowano.**", view=self)

class TruthLieView(ui.View):
    def __init__(self, lie_index, game_key): super().__init__(timeout=180); self.lie_index, self.game_key, self.clicked = lie_index, game_key, False
    async def on_timeout(self):
        if self.game_key in player_games and not self.clicked: del player_games[self.game_key]
    async def check_answer(self, i, choice_index):
        self.clicked=True
        for item in self.children: item.disabled = True
        if choice_index == self.lie_index:
            text = "✅ Brawo! To było kłamstwo! (+5 pkt)"; update_user_score(i.user.id, i.user.name, points=5); await check_and_grant_achievements(i.user, i.channel)
            await post_log("SUCCESS", "Dwie Prawdy (Wygrana)", ctx=i)
        else:
            text = f"❌ Niestety! Kłamstwem było stwierdzenie nr {self.lie_index + 1}."; await post_log("FAIL", "Dwie Prawdy (Przegrana)", ctx=i)
        await i.response.edit_message(content=text, view=self)
        if self.game_key in player_games: del player_games[self.game_key]
    @ui.button(label="1")
    async def b1(self, i, b): await self.check_answer(i, 0)
    @ui.button(label="2")
    async def b2(self, i, b): await self.check_answer(i, 1)
    @ui.button(label="3")
    async def b3(self, i, b): await self.check_answer(i, 2)

@tasks.loop(seconds=30)
async def check_idle_games():
    for cid, game in list(channel_wide_games.items()):
        if time.time() - game.get('last_activity', 0) > IDLE_TIMEOUT:
            if not (ch := bot.get_channel(cid)): del channel_wide_games[cid]; continue
            if game['game_type'] == 'associations':
                async with ch.typing(): word = await generate_from_ai(f'Podaj jedno skojarzenie do "{game["last_word"]}".');
                if word: await ch.send(f"Cisza... może **{word}**? Kto teraz?"); game.update({'last_word': word, 'last_player_id': bot.user.id, 'last_activity': time.time()})
            elif game['game_type'] == 'story':
                async with ch.typing(): sentence = await generate_from_ai(f"Dokończ historię: \"{' '.join(game['full_story'])}\"")
                if sentence: await ch.send(f"*{bot.user.name} dopisuje:*\n> {sentence}"); game.update({'last_player_id': bot.user.id, 'last_activity': time.time()}); game['full_story'].append(sentence)

# --- HANDLERY WIADOMOŚCI ---
async def handle_wordle_guess(msg, game, key):
    guess = msg.content.upper().strip()
    if len(guess) != len(game['word']) or not guess.isalpha(): return
    game['attempts'] += 1; game.setdefault('history', []).append(guess); await msg.reply(f"{check_wordle_guess(guess, game['word'])} `({game['attempts']}/{game['max_attempts']})`", mention_author=False)
    if guess == game['word']:
        points = POINTS[game['difficulty']] + (len(game['word']) - 4) * 5
        await msg.channel.send(f"🎉 Brawo! Słowo: **{game['word']}**! (+{points} pkt)"); update_user_score(msg.author.id, msg.author.name, points=points, wordle_win=True);
        await post_log("SUCCESS", "Wordle (Wygrana)", fields={"Słowo": game['word'], "Próby": f"{game['attempts']}/{game['max_attempts']}", "Punkty": points}, ctx=msg);
        await check_and_grant_achievements(msg.author, msg.channel, wordle_attempts=game['attempts'])
        del player_games[key]
    elif game['attempts'] >= game['max_attempts']:
        await msg.channel.send(f"😔 Niestety. Słowo: **{game['word']}**."); await post_log("FAIL", "Wordle (Przegrana)", fields={"Słowo": game['word']}, ctx=msg); del player_games[key]
async def handle_hangman_guess(msg, game, key):
    guess = msg.content.upper().strip()
    if not guess.isalpha() or len(guess) != 1 or guess in game.get('guessed_letters', []): return
    game.setdefault('guessed_letters', []).append(guess)
    if guess not in game['word']: game['wrong_guesses'] += 1
    await msg.reply(display_hangman(game), mention_author=False)
    if all(l in game['guessed_letters'] for l in game['word']):
        points = POINTS[game['difficulty']]; await msg.channel.send(f"🎉 Gratulacje! Hasło: **{game['word']}** (+{points} pkt)")
        update_user_score(msg.author.id, msg.author.name, points=points, hangman_win=True);
        await post_log("SUCCESS", "Wisielec (Wygrana)", fields={"Hasło": game['word'], "Błędy": f"{game['wrong_guesses']}/{game['max_wrong_guesses']}", "Punkty": points}, ctx=msg)
        await check_and_grant_achievements(msg.author, msg.channel); del player_games[key]
    elif game['wrong_guesses'] >= game['max_wrong_guesses']:
        await msg.channel.send(f"😔 Koniec gry. Hasło: **{game['word']}**."); await post_log("FAIL", "Wisielec (Przegrana)", fields={"Hasło": game['word']}, ctx=msg); del player_games[key]
async def handle_quiz_answer(msg, game, key):
    guess = msg.content.strip().upper()
    if guess not in ["A", "B", "C", "D"] or game.get('answered'): return
    game['answered'] = True; correct_key = game['question_data']['correct_answer']; points = POINTS[game['difficulty']]
    if guess == correct_key:
        await msg.reply(f"✅ Poprawna odpowiedź! (+{points} pkt)", mention_author=False); update_user_score(msg.author.id, msg.author.name, points=points, quiz_win=True)
        await post_log("SUCCESS", "Quiz (Wygrana)", fields={"Kategoria": game.get('category', 'N/A'), "Punkty": points}, ctx=msg)
        await check_and_grant_achievements(msg.author, msg.channel)
    else:
        correct_text = game['question_data']['answers'][correct_key]; await msg.reply(f"❌ Zła odpowiedź. Poprawna: **{correct_key}: {correct_text}**.", mention_author=False)
        await post_log("FAIL", "Quiz (Przegrana)", fields={"Kategoria": game.get('category', 'N/A'), "Odpowiedź": guess, "Poprawna": correct_key}, ctx=msg)
    del player_games[key]
async def handle_20q_question(msg, game, key):
    if game['questions_asked'] >= 20: await msg.reply(f"⌛ Koniec pytań! Odpowiedź: **{game['secret_object']}**.", mention_author=False); await post_log("FAIL", "Zgadnij Co (Przegrana)", {"Obiekt": game['secret_object'], "Powód": "Limit pytań"}, ctx=msg); del player_games[key]; return
    question, game['questions_asked'] = msg.content, game['questions_asked'] + 1
    async with msg.channel.typing(): answer = await answer_yes_no(question, game['secret_object'], game.get('history',[]))
    if answer: await msg.reply(f"`Pyt. {game['questions_asked']}/20`: **{answer}**", mention_author=False); game.setdefault('history', []).append({'q': question, 'a': answer})
    else: await msg.reply("Coś poszło nie tak...", mention_author=False); game['questions_asked'] -= 1
async def handle_association(msg, game):
    if msg.author.id == game.get('last_player_id'): return
    new_word = msg.content.strip().upper().split()[0]
    if not new_word.isalpha() or new_word in game.get('word_history',[]): return
    await msg.reply(f"**{game['last_word']}** → **{new_word}**. OK!", mention_author=False); game.update({'last_word': new_word, 'last_player_id': msg.author.id, 'last_activity': time.time()})
async def handle_story_addition(msg, game):
    if msg.author.id == game.get('last_player_id'): return
    sentence = msg.content.strip()
    if not sentence: return
    game.update({'last_player_id': msg.author.id, 'last_activity': time.time()}); game.setdefault('full_story',[]).append(sentence)
    update_user_score(msg.author.id, msg.author.name, story_post=True); await check_and_grant_achievements(msg.author, msg.channel); await msg.add_reaction('✅')
async def handle_taboo_message(msg, game):
    content_upper = msg.content.upper()
    if msg.author.id == game.get('describing_player_id'):
        forbidden = game.get('taboo_words',[]) + [game.get('keyword')]
        if any(w in re.findall(r'\b\w+\b', content_upper) for w in forbidden):
            used = next((w for w in forbidden if w in re.findall(r'\b\w+\b', content_upper)), ""); await msg.reply(f"🚨 Użyłeś słowa **{used}**! Koniec.")
            await post_log("FAIL", "Tabu (Przegrana)", {"Powód": "Zakazane słowo", "Hasło": game.get('keyword'), "Opisujący": f"<@{game.get('describing_player_id')}>"}, msg); del channel_wide_games[msg.channel.id]
    else:
        if game.get('keyword') in re.findall(r'\b\w+\b', content_upper):
            guesser, describer = msg.author, await bot.fetch_user(game.get('describing_player_id')); await msg.reply(f"🎉 Tak! {guesser.mention} odgadł: **{game['keyword']}**! (+15 pkt!)")
            update_user_score(guesser.id, guesser.name, points=15); await check_and_grant_achievements(guesser, msg.channel, taboo_win=True)
            update_user_score(describer.id, describer.name, points=15); await check_and_grant_achievements(describer, msg.channel, taboo_win=True)
            await post_log("SUCCESS", "Tabu (Wygrana)", {"Hasło": game.get('keyword'), "Zgadujący": f"{guesser.mention}", "Opisujący": f"{describer.mention}"}, msg); del channel_wide_games[msg.channel.id]

      # --- EVENTY BOTA, CHECKI I GŁÓWNE KOMENDY ---
@bot.event
async def on_ready():
    print(f'Zalogowano jako {bot.user}'); setup_database(); check_idle_games.start()
    try: synced = await bot.tree.sync(); print(f"Zsynchronizowano {len(synced)} komend.")
    except Exception as e: print(f"Błąd synchronizacji: {e}")

@bot.event
async def on_message(message):
    if message.author.bot or message.content.startswith('/'): return
    app_info = await bot.application_info()
    maintenance = get_setting('maintenance_mode')
    if maintenance and maintenance['value'] == 'true' and message.author.id != app_info.owner.id: return
    allowed = get_allowed_channels()
    if allowed and message.channel.id not in allowed: return
    key = (message.channel.id, message.author.id)
    if key in player_games:
        game = player_games[key]; handlers = {'wordle': handle_wordle_guess, 'hangman': handle_hangman_guess, 'quiz': handle_quiz_answer, '20_questions': handle_20q_question}
        if game.get('game_type') in handlers: await handlers[game['game_type']](message, game, key); return
    if message.channel.id in channel_wide_games:
        game = channel_wide_games[message.channel.id]; handlers = {'associations': handle_association, 'story': handle_story_addition, 'taboo': handle_taboo_message}
        if game.get('game_type') in handlers: await handlers[game.get('game_type')](message, game)

@bot.tree.error
async def on_app_command_error(i: discord.Interaction, error: app_commands.AppCommandError):
    err = error.original if hasattr(error, 'original') else error
    await post_log("ERROR", f"Błąd w komendzie: /{i.command.name if i.command else 'Nieznana'}", desc=f"```python\n{type(err).__name__}: {err}\n```", ctx=i)
    if not i.response.is_done(): await i.response.send_message("Ups! Coś poszło nie tak.", ephemeral=True)
    else: await i.followup.send("Ups! Coś poszło nie tak.", ephemeral=True)

async def is_bot_owner(i: discord.Interaction) -> bool: app_info = await i.client.application_info(); return i.user.id == app_info.owner.id
def is_admin(): return app_commands.check(lambda i: i.user.guild_permissions.administrator)
async def check_channel_and_game(i: discord.Interaction, player_game: bool):
    maintenance = get_setting('maintenance_mode'); app_info = await bot.application_info()
    if maintenance and maintenance['value'] == 'true' and i.user.id != app_info.owner.id:
        await i.response.send_message("🛠️ Bot jest w trybie konserwacji.", ephemeral=True); return False
    allowed = get_allowed_channels()
    if allowed and i.channel.id not in allowed:
        await i.response.send_message("Bota można używać tylko na wyznaczonych kanałach.", ephemeral=True); return False
    if player_game and (i.channel.id, i.user.id) in player_games:
        await i.response.send_message("Masz już grę osobistą. Użyj `/koniec`.", ephemeral=True); return False
    elif not player_game and i.channel.id in channel_wide_games:
        await i.response.send_message(f"Gra (`{channel_wide_games[i.channel.id].get('game_type')}`) już trwa.", ephemeral=True); return False
    return True

@bot.tree.command(name="info", description="Wyświetla listę gier i komend.")
async def info(i: discord.Interaction):
    embed = discord.Embed(title=f"👋 Witaj! Jestem {bot.user.name}", description="Bot do gier oparty na AI.", color=0x3498db).set_thumbnail(url=bot.user.display_avatar.url)
    embed.add_field(name="👤 Gry Osobiste", value="`/wordle`, `/wisielec`, `/quiz`, `/dwie_prawdy`, `/zgadnij_co`", inline=False)
    embed.add_field(name="👥 Gry Grupowe", value="`/skojarzenia`, `/historia`, `/tabu`, `/scenariusz`", inline=False)
    embed.add_field(name="🛠️ Komendy", value="`/ranking`, `/profil`, `/osiagniecia`, `/podpowiedz`, `/koniec`, `/koniec_kanal` (admin)", inline=False)
    embed.set_footer(text=f"Wersja bota: 3.2"); await i.response.send_message(embed=embed)

@bot.tree.command(name="wordle", description="Rozpocznij osobistą grę w Wordle.")
@app_commands.describe(długość="Dł. słowa (4-8)", trudność="Poziom trudności")
@app_commands.choices(trudność=[app_commands.Choice(name=v.title(), value=v) for v in ["łatwy", "normalny", "trudny"]])
async def wordle(i: discord.Interaction, długość: app_commands.Range[int, 4, 8] = 5, trudność: str = "normalny"):
    if not await check_channel_and_game(i, True): return
    await i.response.send_message("🤖 Generuję słowo...", ephemeral=True); word = await generate_word(długość, trudność)
    if not word: return await i.followup.send("Błąd AI.", ephemeral=True)
    player_games[(i.channel.id, i.user.id)] = {'game_type': 'wordle', 'word': word, 'attempts': 0, 'max_attempts': 6, 'difficulty': trudność, 'hints_used': 0}
    await post_log("INFO", "Rozpoczęto: Wordle", fields={"Gracz": i.user.mention, "Parametry": f"Dł: {długość}, Tr: {trudność}", "Słowo": f"||{word}||"}, ctx=i)
    await i.followup.send(f"✅ **Twoja gra, {i.user.mention}!** Masz 6 prób.", ephemeral=False)

@bot.tree.command(name="wisielec", description="Rozpocznij osobistą grę w wisielca.")
@app_commands.choices(trudność=[app_commands.Choice(name=v.title(), value=v) for v in ["łatwy", "normalny", "trudny"]])
async def hangman(i: discord.Interaction, trudność: str = "normalny"):
    if not await check_channel_and_game(i, True): return
    await i.response.send_message("🤖 Generuję hasło...", ephemeral=True); word = await generate_word(random.randint(5, 8), trudność)
    if not word: return await i.followup.send("Błąd AI.", ephemeral=True)
    game = {'game_type': 'hangman', 'word': word, 'guessed_letters': [], 'wrong_guesses': 0, 'max_wrong_guesses': 6, 'difficulty': trudność, 'hints_used': 0}
    player_games[(i.channel.id, i.user.id)] = game
    await post_log("INFO", "Rozpoczęto: Wisielec", fields={"Gracz": i.user.mention, "Trudność": trudność, "Słowo": f"||{word}||"}, ctx=i)
    await i.followup.send(f"✅ **Twój Wisielec, {i.user.mention}!**\n" + display_hangman(game))

@bot.tree.command(name="quiz", description="Rozpocznij osobisty quiz.")
@app_commands.describe(kategoria="Kategoria", trudność="Poziom trudności")
@app_commands.choices(trudność=[app_commands.Choice(name=v.title(), value=v) for v in ["łatwy", "normalny", "trudny"]])
async def quiz(i: discord.Interaction, kategoria: str, trudność: str = "normalny"):
    if not await check_channel_and_game(i, True): return
    await i.response.send_message(f"🤖 Myślę nad pytaniem...", ephemeral=True); data = await generate_quiz_question(kategoria, trudność)
    if not data: return await i.followup.send("Nie udało się wygenerować pytania.", ephemeral=True)
    player_games[(i.channel.id, i.user.id)] = {'game_type': 'quiz', 'question_data': data, 'answered': False, 'difficulty': trudność, 'category': kategoria}
    embed = discord.Embed(title=f"🧠 Twój QUIZ: {kategoria.title()}", description=data.get('question'), color=discord.Color.blue())
    for key, value in data.get('answers', {}).items(): embed.add_field(name=f"**{key}**", value=value, inline=False)
    await post_log("INFO", "Rozpoczęto: Quiz", fields={"Gracz": i.user.mention, "Parametry": f"Kat: {kategoria}, Tr: {trudność}"}, ctx=i)
    await i.followup.send(f"{i.user.mention}, Twoje pytanie:", embed=embed)

@bot.tree.command(name="dwie_prawdy", description="Zagraj w Dwie Prawdy i Kłamstwo.")
async def two_truths(i: discord.Interaction):
    if not await check_channel_and_game(i, True): return
    await i.response.send_message("🤖 Myślę nad historiami...", ephemeral=True); data = await generate_from_ai('Stwórz 3 stwierdzenia o sobie (AI): 2 prawdziwe, 1 kłamstwo. JSON: {"statements": ["...", "..."], "lie_index": 1}', is_json=True)
    if not data: return await i.followup.send("Błąd AI.", ephemeral=True)
    key = (i.channel.id, i.user.id); player_games[key] = {'game_type': 'two_truths'}
    desc = f"Zgadnij fałsz!\n\n1. {data['statements'][0]}\n2. {data['statements'][1]}\n3. {data['statements'][2]}"
    embed = discord.Embed(title="🕵️ Dwie Prawdy i Kłamstwo", description=desc, color=discord.Color.purple())
    await post_log("INFO", "Rozpoczęto: Dwie Prawdy", fields={"Gracz": i.user.mention}, ctx=i)
    await i.followup.send(f"{i.user.mention}, Twoja gra:", embed=embed, view=TruthLieView(data['lie_index'], key))

@bot.tree.command(name="zgadnij_co", description="Rozpocznij grę w 20 pytań.")
@app_commands.describe(kategoria="Kategoria obiektu")
async def twenty_questions(i: discord.Interaction, kategoria: str):
    if not await check_channel_and_game(i, True): return
    await i.response.send_message("🤔 Myślę o czymś...", ephemeral=True); secret = await generate_from_ai(f"Podaj jeden rzeczownik z kategorii '{kategoria}'.")
    if not secret: return await i.followup.send("Błąd AI.", ephemeral=True)
    player_games[(i.channel.id, i.user.id)] = {'game_type': '20_questions', 'secret_object': secret.upper(), 'questions_asked': 0, 'history': [], 'hints_used': 0}
    await post_log("INFO", "Rozpoczęto: Zgadnij Co", fields={"Gracz": i.user.mention, "Kategoria": kategoria, "Obiekt": f"||{secret.upper()}||"}, ctx=i)
    await i.followup.send(f"✅ {i.user.mention}, pomyślałem o czymś! Masz 20 pytań.")

@bot.tree.command(name="skojarzenia", description="Rozpocznij grę w skojarzenia.")
async def associations(i: discord.Interaction):
    if not await check_channel_and_game(i, False): return
    await i.response.send_message("🤖 Losuję słowo..."); word = await generate_word(random.randint(4, 7), "normalny")
    if not word: return await i.edit_original_response(content="Błąd AI.")
    channel_wide_games[i.channel.id] = {'game_type': 'associations', 'last_word': word, 'last_player_id': bot.user.id, 'last_activity': time.time()}
    await post_log("INFO", "Rozpoczęto: Skojarzenia", fields={"Rozpoczął": i.user.mention, "Kanał": i.channel.mention}, ctx=i)
    await i.edit_original_response(content=f"**Skojarzenia**! Słowo: **{word}**")

@bot.tree.command(name="historia", description="Rozpocznij wspólną historię.")
async def story(i: discord.Interaction, temat: str):
    if not await check_channel_and_game(i, False): return
    await i.response.send_message(f"✍️ Myślę nad początkiem..."); sentence = await generate_from_ai(f"Napisz zdanie rozpoczynające historię o: '{temat}'.")
    if not sentence: return await i.edit_original_response(content="Błąd AI.")
    channel_wide_games[i.channel.id] = {'game_type': 'story', 'full_story': [sentence], 'last_player_id': bot.user.id, 'last_activity': time.time()}
    await post_log("INFO", "Rozpoczęto: Historia", fields={"Rozpoczął": i.user.mention, "Temat": temat}, ctx=i)
    await i.edit_original_response(content=f"**Wspólne pisanie**! Początek:\n> {sentence}")

@bot.tree.command(name="tabu", description="Rozpocznij grę w Tabu.")
async def taboo(i: discord.Interaction, gracz: discord.Member):
    if not await check_channel_and_game(i, False): return
    if gracz.bot: return await i.response.send_message("Nie możesz wyznaczyć bota!", ephemeral=True)
    await i.response.send_message(f"🤖 Generuję kartę dla {gracz.mention}..."); card = await generate_from_ai('Stwórz kartę Tabu: słowo kluczowe i 5 zakazanych. JSON: {"keyword": "PSZCZOŁA", "taboo_words": ["MIÓD", "UL"]}', is_json=True)
    if not card: return await i.edit_original_response(content="Błąd AI.")
    channel_wide_games[i.channel.id] = {'game_type': 'tabu', 'keyword': card['keyword'], 'taboo_words': card['taboo_words'], 'describing_player_id': gracz.id}
    try:
        embed = discord.Embed(title="🤫 Twoja Karta Tabu", description=f"Opiisz: **{card['keyword']}**.", color=discord.Color.orange())
        embed.add_field(name="Zakazane:", value="- " + "\n- ".join(card['taboo_words'])); await gracz.send(embed=embed)
        await i.edit_original_response(content=f"✅ Karta wysłana do {gracz.mention}!"); await post_log("INFO", "Rozpoczęto: Tabu", fields={"Opisujący": gracz.mention, "Hasło": f"||{card['keyword']}||"}, ctx=i)
    except discord.Forbidden: del channel_wide_games[i.channel.id]; await i.edit_original_response(content=f"⚠️ {gracz.mention} ma zablokowane DM-y.")

@bot.tree.command(name="scenariusz", description="Generuje kreatywny scenariusz.")
async def scenario(i: discord.Interaction):
    await i.response.send_message("🤔 Tworzę scenariusz..."); text = await generate_from_ai('Stwórz kreatywny scenariusz "Co byś zrobił, gdyby...".')
    await i.edit_original_response(content=f"**Co byś zrobił, gdyby...**\n> {text or 'Błąd AI'}")

@bot.tree.command(name="ranking", description="Wyświetla top 10 graczy.")
async def ranking(i: discord.Interaction):
    lb = get_leaderboard(); embed = discord.Embed(title="🏆 Ranking Serwera", color=discord.Color.gold())
    if not lb: embed.description = "Ranking jest pusty!"
    else: embed.description = "\n".join([f"{idx+1}. {row['user_name']} - {row['score']} pkt" for idx, row in enumerate(lb)])
    await i.response.send_message(embed=embed)
    
@bot.tree.command(name="profil", description="Wyświetla statystyki gracza.")
async def profile(i: discord.Interaction, użytkownik: discord.Member = None):
    user = użytkownik or i.user; stats = get_user_stats(user.id)
    if not stats: return await i.response.send_message(f"{user.name} nie ma statystyk.", ephemeral=True)
    embed = discord.Embed(title=f"📊 Profil: {user.name}", color=discord.Color.teal()).set_thumbnail(url=user.display_avatar.url)
    embed.add_field(name="Punkty", value=stats['score']); embed.add_field(name="Quizy", value=stats['quiz_wins']); embed.add_field(name="Wordle", value=stats['wordle_wins'])
    if achs := get_user_achievements(user.id): embed.add_field(name="🏆 Osiągnięcia", value="\n".join([f"**{ACHIEVEMENTS[a['achievement_id']]['name']}**" for a in achs]), inline=False)
    await i.response.send_message(embed=embed)

@bot.tree.command(name="osiagniecia", description="Wyświetla listę osiągnięć.")
async def achievements_list(i: discord.Interaction):
    user_achs = [a['achievement_id'] for a in get_user_achievements(i.user.id)]; embed = discord.Embed(title="🏆 Dostępne Osiągnięcia", color=discord.Color.gold())
    embed.description = "\n".join([f"{'✅' if id in user_achs else '❌'} **{data['name']}**: *{data['description']}*" for id, data in ACHIEVEMENTS.items()])
    await i.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="podpowiedz", description="Daje podpowiedź w twojej grze.")
async def hint(i: discord.Interaction):
    game = player_games.get((i.channel.id, i.user.id))
    if not game or game.get('hints_used', 0) > 0: return await i.response.send_message("Nie masz gry do podpowiedzi lub już ją wykorzystałeś.", ephemeral=True)
    if game['game_type'] == 'hangman':
        if game['wrong_guesses'] >= game['max_wrong_guesses'] - 1: return await i.response.send_message("Za późno!", ephemeral=True)
        unrevealed = [l for l in game['word'] if l not in game.get('guessed_letters',[])]
        if unrevealed: game.setdefault('guessed_letters',[]).append(random.choice(unrevealed)); game['wrong_guesses']+=1; game['hints_used']=1; await i.response.send_message("💡 Odsłaniam literę! (Koszt: 1 błąd)", ephemeral=True); await i.channel.send(display_hangman(game))
    elif game['game_type'] == '20_questions':
        game['hints_used']=1; game['questions_asked']+=2; await i.response.defer(ephemeral=True); text = await generate_hint(game['secret_object']); await i.followup.send(f"💡 Podpowiedź (koszt: 2 pytania): **{text or 'Brak'}**")
    elif game['game_type'] == 'wordle':
        game['hints_used']=1; game['attempts']+=1
        green = {game['history'][j][idx] for j in range(len(game.get('history',[]))) for idx in range(len(game['word'])) if game['history'][j][idx] == game['word'][idx]}
        pool = list(set(game['word']) - green)
        if pool: await i.response.send_message(f"💡 Litera **{random.choice(pool)}** jest w słowie. (Koszt: 1 próba)", ephemeral=True)
        else: await i.response.send_message("Brak liter do podpowiedzenia!", ephemeral=True)
    else: await i.response.send_message("Ta gra nie obsługuje podpowiedzi.", ephemeral=True)

@bot.tree.command(name="odgaduje", description="Podaj ostateczną odpowiedź w 'Zgadnij Co'.")
async def guess(i: discord.Interaction, próba: str):
    key, game = (i.channel.id, i.user.id), player_games.get((i.channel.id, i.user.id))
    if not game or game.get('game_type') != '20_questions': return await i.response.send_message("Tylko w 'Zgadnij Co'.", ephemeral=True)
    points = POINTS['normalny'] + 10
    if próba.upper() == game['secret_object']:
        await i.response.send_message(f"🎉 Niesamowite! Odpowiedź to **{game['secret_object']}**! (+{points} pkt)")
        update_user_score(i.user.id, i.user.name, points=points); await check_and_grant_achievements(i.user, i.channel, **{'20q_win': True, 'questions_asked': game['questions_asked']})
        await post_log("SUCCESS", "Zgadnij Co (Wygrana)", fields={"Obiekt": game['secret_object'], "Pytania": game['questions_asked'], "Punkty": points}, ctx=i); del player_games[key]
    else: game['questions_asked']+=1; await i.response.send_message(f"❌ Niestety, to nie **{próba.upper()}**. (Pytanie {game['questions_asked']}/20)"); await post_log("INFO", "Zgadnij Co (Zła próba)", fields={"Próba": próba}, ctx=i)

@bot.tree.command(name="koniec", description="Zakończ swoją grę osobistą.")
async def stop_my_game(i: discord.Interaction):
    game = player_games.pop((i.channel.id, i.user.id), None)
    if game:
        msg = f"Twoja gra (`{game.get('game_type')}`) została zakończona."
        if 'word' in game: msg += f" Słowo: **{game['word']}**."
        await i.response.send_message(msg, ephemeral=True)
        await post_log("INFO", f"Gra Zakończona Ręcznie", desc=f"{i.user.mention} zakończył swoją grę.", fields={"Gra": game.get('game_type')}, ctx=i)
    else: await i.response.send_message("Nie masz aktywnej gry.", ephemeral=True)

@bot.tree.command(name="koniec_kanal", description="[Admin] Zakończ grę grupową.")
@is_admin()
async def stop_channel_game(i: discord.Interaction):
    game = channel_wide_games.pop(i.channel.id, None)
    if game:
        await i.response.send_message(f"Gra (`{game.get('game_type')}`) zakończona.")
        await post_log("WARNING", f"Gra Zakończona przez Admina", desc=f"{i.user.mention} zakończył grę.", fields={"Gra": game.get('game_type')}, ctx=i)
    else: await i.response.send_message("Brak gry grupowej.", ephemeral=True)

@bot.tree.command(name="historia_koniec", description="Zakończ i wyświetl historię.")
async def story_end(i: discord.Interaction):
    game = channel_wide_games.pop(i.channel.id, None)
    if game and game.get('game_type') == 'story':
        embed = discord.Embed(title="Oto Wasza Historia!", description=" ".join(game['full_story']), color=discord.Color.green())
        await i.response.send_message(embed=embed)
        await post_log("INFO", "Zakończono: Historia", desc=f"Zakończona przez {i.user.mention}.", ctx=i)
    else: await i.response.send_message("Nie jest tworzona żadna historia.", ephemeral=True)

@bot.tree.command(name="ustaw_kanal", description="[Admin] Dodaje ten kanał do dozwolonych.")
@is_admin()
async def set_channel(i: discord.Interaction):
    channels = get_allowed_channels(); channels.append(i.channel.id); set_allowed_channels(channels)
    await i.response.send_message(f"✅ Kanał {i.channel.mention} dodany.", ephemeral=True)
        
@bot.tree.command(name="usun_kanal", description="[Admin] Usuwa ten kanał z dozwolonych.")
@is_admin()
async def remove_channel(i: discord.Interaction):
    channels = get_allowed_channels()
    if i.channel.id in channels: channels.remove(i.channel.id); set_allowed_channels(channels); await i.response.send_message(f"✅ Kanał {i.channel.mention} usunięty.", ephemeral=True)
    else: await i.response.send_message("Tego kanału nie ma na liście.", ephemeral=True)
        
@bot.tree.command(name="db_reset_ranking", description="[Właściciel] Resetuje ranking.")
@app_commands.check(is_bot_owner)
async def db_reset_ranking(i: discord.Interaction):
    view = ConfirmResetView(i.user.id); await i.response.send_message(embed=discord.Embed(title="🚨 Potwierdzenie", description="Czy na pewno chcesz usunąć WSZYSTKIE punkty i osiągnięcia?", color=discord.Color.red()), view=view, ephemeral=True)
    await view.wait()
    if view.confirmed:
        try:
            with get_db_connection() as conn:
                with conn.cursor() as cur: cur.execute("DELETE FROM users"); cur.execute("DELETE FROM achievements"); conn.commit()
            await i.edit_original_response(embed=discord.Embed(title="✔️ Reset Zakończony", color=discord.Color.green()), view=None)
            await post_log("WARNING", "Zresetowano Ranking", desc=f"Ranking zresetowany przez {i.user.mention}.", ctx=i); await i.channel.send("📢 Ranking został zresetowany!")
        except Exception as e: await i.edit_original_response(embed=discord.Embed(title="❌ Błąd", description=f"`{e}`", color=discord.Color.dark_red()), view=None)

@db_reset_ranking.error
async def on_db_reset_error(i, error):
    if isinstance(error, app_commands.CheckFailure): await i.response.send_message("⛔ Tylko dla właściciela.", ephemeral=True)
    else: await i.response.send_message(f"Błąd: {error}", ephemeral=True)
    
@bot.tree.command(name="maintenance", description="[Właściciel] Tryb konserwacji.")
@app_commands.choices(status=[app_commands.Choice(name="ON", value="true"), app_commands.Choice(name="OFF", value="false")])
@app_commands.check(is_bot_owner)
async def maintenance_mode(i: discord.Interaction, status: str):
    is_on = (status == 'true'); set_setting('maintenance_mode', status)
    await i.response.send_message(f"🔧 Tryb konserwacji **{'WŁĄCZONY' if is_on else 'WYŁĄCZONY'}**.", ephemeral=True)
    await post_log("WARNING", "Zmieniono Tryb Konserwacji", desc=f"Tryb konserwacji: **{'WŁĄCZONY' if is_on else 'WYŁĄCZONY'}**.", ctx=i)
    await set_channels_lock(lock_status=is_on, guild=i.guild, interaction=i)
    
    embed = discord.Embed(title="🛠️ Przerwa Techniczna" if is_on else "✅ Koniec Przerwy", color=discord.Color.orange() if is_on else discord.Color.green())
    embed.description = "Pisanie i gra są **zablokowane**." if is_on else "Funkcje zostały **przywrócone**."
    for cid in get_allowed_channels() or [i.channel.id]:
        if ch := bot.get_channel(cid):
            try: await ch.send(embed=embed)
            except discord.Forbidden: pass
        
# --- URUCHOMIENIE BOTA ---
bot.run(DISCORD_TOKEN)
