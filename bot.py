import discord
from discord.ext import commands
from discord import app_commands
import sqlite3
import asyncio
import csv
import json
import io
import os
import re
import aiohttp
import traceback
from datetime import datetime

# ══════════════════════════════════════════════════════════
#  CONFIG
# ══════════════════════════════════════════════════════════
TOKEN             = "MTQ4ODg4NzAyMDcwODQzMzk5MQ.G_3CGr.U9k-4GxMQknljoR6QWgIKQFYYlj4x7wKpS-Dwo"
BASE_DIR          = os.path.dirname(os.path.abspath(__file__))
DB_PATH           = os.path.join(BASE_DIR, "personnes.db")
GUILD_ID          = 1481774540013961408
OWNER_IDS         = [1455157100413063346]
IMPORT_CHANNEL_ID = 1481774542870544467
LOCAL_DATA_DIR    = os.path.join(BASE_DIR, "data")
AUTO_IMPORT_ON_START = True
MAX_LOCAL_FILE_SIZE_MB = 0  # 0 = pas de limite
# ══════════════════════════════════════════════════════════

os.makedirs(LOCAL_DATA_DIR, exist_ok=True)

# ── Mapping colonnes → champs DB ──────────────────────────
FIELD_KEYS = {
    "prenom":         ["prenom", "prénom", "firstname", "first_name", "fname", "given_name", "givenname"],
    "nom":            ["nom", "name", "lastname", "last_name", "lname", "surname", "family_name", "familyname", "nomdefamille"],
    "telephone":      ["telephone", "téléphone", "tel", "tél", "phone", "mobile", "portable", "gsm", "num", "numero", "numéro", "phonenumber", "phone_number"],
    "email":          ["email", "e-mail", "mail", "courriel", "emailaddress", "email_address"],
    "adresse":        ["adresse", "address", "addr", "rue", "street", "voie"],
    "ville":          ["ville", "city", "localite", "localité", "commune", "town", "municipality"],
    "code_postal":    ["code_postal", "codepostal", "cp", "zip", "zipcode", "zip_code", "postalcode", "postal_code", "npa"],
    "pays":           ["pays", "country", "nation"],
    "date_naissance": ["date_naissance", "datenaissance", "birthday", "birth_date", "birthdate", "dob", "naissance"],
    "sexe":           ["sexe", "genre", "gender", "sex", "civilite", "civilité"],
    "ip":             ["ip", "ip_address", "ipaddress", "adresse_ip"],
    "notes":          ["notes", "note", "commentaire", "commentaires", "info", "infos", "remarque"],
}

def find_key(d: dict, candidates: list):
    for k in candidates:
        for dk in d.keys():
            if dk.strip().lower() == k:
                return dk
    return None

def flatten_record(obj, out=None):
    """Aplatit un dict JSON imbriqué en clés simples + chemins."""
    if out is None:
        out = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, dict):
                flatten_record(v, out)
            elif isinstance(v, list):
                # Conserve les listes simples sous forme texte utile en debug/notes.
                if v and all(not isinstance(x, (dict, list)) for x in v):
                    out.setdefault(str(k).strip().lower(), ", ".join(map(str, v)))
            else:
                key = str(k).strip().lower()
                if key not in out:
                    out[key] = v
    return out

# ══════════════════════════════════════════════════════════
#  DB
# ══════════════════════════════════════════════════════════
def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS personnes (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            prenom        TEXT,
            nom           TEXT,
            telephone     TEXT,
            email         TEXT,
            adresse       TEXT,
            ville         TEXT,
            code_postal   TEXT,
            pays          TEXT,
            date_naissance TEXT,
            sexe          TEXT,
            ip            TEXT,
            notes         TEXT,
            source        TEXT,
            created_at    TEXT DEFAULT (datetime('now'))
        )
    """)
    # Migration : ajoute les nouvelles colonnes si ancienne DB
    existing = [row[1] for row in conn.execute("PRAGMA table_info(personnes)").fetchall()]
    new_cols = {
        "telephone": "TEXT", "email": "TEXT", "adresse": "TEXT",
        "ville": "TEXT", "code_postal": "TEXT", "pays": "TEXT",
        "date_naissance": "TEXT", "sexe": "TEXT", "ip": "TEXT",
        "notes": "TEXT", "source": "TEXT",
        "created_at": "TEXT DEFAULT (datetime('now'))",
    }
    for col, typ in new_cols.items():
        if col not in existing:
            conn.execute(f"ALTER TABLE personnes ADD COLUMN {col} {typ}")
    conn.commit()
    conn.close()

init_db()

def insert_batch(rows: list, source: str = "") -> int:
    conn = sqlite3.connect(DB_PATH)
    for r in rows:
        conn.execute("""
            INSERT INTO personnes
                (prenom, nom, telephone, email, adresse, ville, code_postal,
                 pays, date_naissance, sexe, ip, notes, source)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            r.get("prenom") or None, r.get("nom") or None,
            r.get("telephone") or None, r.get("email") or None,
            r.get("adresse") or None, r.get("ville") or None,
            r.get("code_postal") or None, r.get("pays") or None,
            r.get("date_naissance") or None, r.get("sexe") or None,
            r.get("ip") or None, r.get("notes") or None,
            source or None,
        ))
    conn.commit()
    n = conn.total_changes
    conn.close()
    return n

def count_db() -> int:
    conn = sqlite3.connect(DB_PATH)
    n = conn.execute("SELECT COUNT(*) FROM personnes").fetchone()[0]
    conn.close()
    return n

def delete_person(pid: int) -> bool:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("DELETE FROM personnes WHERE id = ?", (pid,))
    ok = conn.total_changes > 0
    conn.commit()
    conn.close()
    return ok

def get_by_id(pid: int):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    r = conn.execute("SELECT * FROM personnes WHERE id = ?", (pid,)).fetchone()
    conn.close()
    return dict(r) if r else None

def search_advanced(prenom="", nom="", telephone="", email="",
                    ville="", code_postal="", ip="",
                    query_global="", limit=20) -> list:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conditions, params = [], []

    def add(field, value):
        if value and value.strip():
            conditions.append(f"LOWER(COALESCE({field},'')) LIKE ?")
            params.append(f"%{value.strip().lower()}%")

    if query_global.strip():
        fields = ["prenom","nom","telephone","email","adresse","ville",
                  "code_postal","pays","date_naissance","sexe","ip","notes"]
        sub = " OR ".join([f"LOWER(COALESCE({f},'')) LIKE ?" for f in fields])
        conditions.append(f"({sub})")
        q = f"%{query_global.strip().lower()}%"
        params.extend([q] * len(fields))
    else:
        add("prenom", prenom); add("nom", nom)
        add("telephone", telephone); add("email", email)
        add("ville", ville); add("code_postal", code_postal)
        add("ip", ip)

    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    rows = [dict(r) for r in conn.execute(
        f"SELECT * FROM personnes {where} ORDER BY id DESC LIMIT ?",
        params + [limit]
    ).fetchall()]
    conn.close()
    return rows

# ══════════════════════════════════════════════════════════
#  PARSERS
# ══════════════════════════════════════════════════════════
def extract_row(raw: dict) -> dict:
    if not isinstance(raw, dict):
        return {}
    flat = flatten_record(raw)
    out = {}
    for field, candidates in FIELD_KEYS.items():
        k = find_key(flat, candidates)
        if k is not None:
            out[field] = str(flat[k]).strip() if flat[k] else None
    return out

def parse_csv_bytes(data: bytes) -> list:
    text = data.decode("utf-8-sig", errors="ignore")
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=',;\t|')
    except Exception:
        dialect = csv.excel
    return [extract_row(row) for row in csv.DictReader(io.StringIO(text), dialect=dialect)]

def parse_jsonl_bytes(data: bytes) -> list:
    rows = []
    for line in data.decode("utf-8", errors="ignore").splitlines():
        line = line.strip()
        if line:
            try:
                payload = json.loads(line)
                if isinstance(payload, dict):
                    r = extract_row(payload)
                    if r:
                        rows.append(r)
                elif isinstance(payload, list):
                    for item in payload:
                        if isinstance(item, dict):
                            r = extract_row(item)
                            if r:
                                rows.append(r)
            except Exception:
                pass
    return rows

def parse_json_bytes(data: bytes) -> list:
    d = json.loads(data.decode("utf-8", errors="ignore"))
    items = d if isinstance(d, list) else next((v for v in d.values() if isinstance(v, list)), [])
    return [extract_row(obj) for obj in items if isinstance(obj, dict)]

def parse_txt_bytes(data: bytes) -> list:
    rows = []
    for line in data.decode("utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line:
            continue
        # Certains dumps .txt contiennent du JSON par ligne (dict ou liste).
        try:
            payload = json.loads(line)
            if isinstance(payload, dict):
                r = extract_row(payload)
                if r:
                    rows.append(r)
                continue
            if isinstance(payload, list):
                for item in payload:
                    if isinstance(item, dict):
                        r = extract_row(item)
                        if r:
                            rows.append(r)
                continue
        except Exception:
            pass
        for sep in [',', ';', '\t']:
            if sep in line:
                p = [x.strip() for x in line.split(sep)]
                rows.append({"prenom": p[0], "nom": p[1] if len(p) > 1 else None})
                break
        else:
            p = line.split(' ', 1)
            if len(p) == 2:
                rows.append({"prenom": p[0].strip(), "nom": p[1].strip()})
    return rows

def parse_txt_line(line: str) -> list:
    """Parse une seule ligne txt vers 0..n lignes normalisées."""
    line = (line or "").strip()
    if not line:
        return []
    out = []
    try:
        payload = json.loads(line)
        if isinstance(payload, dict):
            r = extract_row(payload)
            if r:
                out.append(r)
            return out
        if isinstance(payload, list):
            for item in payload:
                if isinstance(item, dict):
                    r = extract_row(item)
                    if r:
                        out.append(r)
            return out
    except Exception:
        pass
    for sep in [',', ';', '\t']:
        if sep in line:
            p = [x.strip() for x in line.split(sep)]
            out.append({"prenom": p[0], "nom": p[1] if len(p) > 1 else None})
            return out
    p = line.split(' ', 1)
    if len(p) == 2:
        out.append({"prenom": p[0].strip(), "nom": p[1].strip()})
    return out

PARSERS = {".csv": parse_csv_bytes, ".jsonl": parse_jsonl_bytes,
           ".json": parse_json_bytes, ".txt": parse_txt_bytes}

# ══════════════════════════════════════════════════════════
#  EMBEDS
# ══════════════════════════════════════════════════════════
COLORS = {"blue": 0x5865F2, "green": 0x2ecc71, "orange": 0xf39c12,
          "red": 0xe74c3c, "purple": 0x9b59b6}
BRAND_NAME = "Lookup Bot"
BRAND_ICON = "https://cdn.discordapp.com/embed/avatars/0.png"

def v(val) -> str:
    return str(val).strip() if val else "—"

def styled_embed(title: str, color_key: str = "blue", description: str = "") -> discord.Embed:
    e = discord.Embed(title=title, description=description, color=COLORS.get(color_key, COLORS["blue"]))
    e.timestamp = datetime.utcnow()
    e.set_author(name=BRAND_NAME, icon_url=BRAND_ICON)
    return e

def build_person_embed(r: dict, index: int = 1, total: int = 1) -> discord.Embed:
    prenom = v(r.get("prenom"))
    nom    = v(r.get("nom"))
    title  = f"👤 {prenom} {nom}".strip()
    if title == "👤 — —":
        title = f"👤 Entrée #{r['id']}"

    embed = styled_embed(title, "blue")

    # Identité
    id_lines = []
    if r.get("prenom"):         id_lines.append(f"**Prénom :** {r['prenom']}")
    if r.get("nom"):            id_lines.append(f"**Nom :** {r['nom']}")
    if r.get("sexe"):           id_lines.append(f"**Sexe :** {r['sexe']}")
    if r.get("date_naissance"): id_lines.append(f"**Naissance :** {r['date_naissance']}")
    if id_lines:
        embed.add_field(name="🪪  Identité", value="\n".join(id_lines), inline=False)

    # Contact
    ct_lines = []
    if r.get("telephone"): ct_lines.append(f"**Téléphone :** `{r['telephone']}`")
    if r.get("email"):     ct_lines.append(f"**Email :** `{r['email']}`")
    if ct_lines:
        embed.add_field(name="📞  Contact", value="\n".join(ct_lines), inline=False)

    # Localisation
    loc_lines = []
    if r.get("adresse"):     loc_lines.append(f"**Adresse :** {r['adresse']}")
    if r.get("code_postal"): loc_lines.append(f"**Code postal :** {r['code_postal']}")
    if r.get("ville"):       loc_lines.append(f"**Ville :** {r['ville']}")
    if r.get("pays"):        loc_lines.append(f"**Pays :** {r['pays']}")
    if loc_lines:
        embed.add_field(name="📍  Localisation", value="\n".join(loc_lines), inline=False)

    # Technique
    tech_lines = []
    if r.get("ip"):     tech_lines.append(f"**IP :** `{r['ip']}`")
    if r.get("source"): tech_lines.append(f"**Source :** {r['source']}")
    if tech_lines:
        embed.add_field(name="🖥️  Technique", value="\n".join(tech_lines), inline=False)

    # Notes
    if r.get("notes"):
        embed.add_field(name="📝  Notes", value=r["notes"][:500], inline=False)

    embed.set_thumbnail(url=BRAND_ICON)
    embed.set_footer(text=f"ID #{r['id']}  •  Résultat {index}/{total}  •  Ajout: {r.get('created_at','?')}")
    return embed

def build_summary_embed(rows: list, query_desc: str) -> discord.Embed:
    embed = styled_embed(f"🔍 Résultats — {query_desc}", "blue")
    lines = []
    for r in rows:
        prenom = r.get("prenom") or ""
        nom    = r.get("nom") or ""
        tel    = r.get("telephone") or ""
        email  = r.get("email") or ""
        ville  = r.get("ville") or ""
        line   = f"`#{r['id']}` **{(prenom+' '+nom).strip() or '—'}**"
        if tel:   line += f"  📞 `{tel}`"
        if email: line += f"  ✉️ `{email}`"
        if ville: line += f"  📍 {ville}"
        lines.append(line)
    embed.description = f"**{len(rows)}** résultat(s)\n\n" + "\n".join(lines)
    embed.set_thumbnail(url=BRAND_ICON)
    embed.set_footer(text="⬅️ ➡️ Naviguer  •  📋 Vue liste")
    return embed

# ══════════════════════════════════════════════════════════
#  URL HELPERS
# ══════════════════════════════════════════════════════════
async def resolve_gofile(url: str, session: aiohttp.ClientSession):
    match = re.search(r"gofile\.io/d/([A-Za-z0-9]+)", url)
    if not match:
        raise ValueError("Lien Gofile invalide.")
    cid = match.group(1)
    async with session.post("https://api.gofile.io/accounts") as r:
        d = await r.json()
        if d.get("status") != "ok":
            raise ValueError(f"Gofile token : {d.get('status')}")
        token = d["data"]["token"]
    async with session.get(f"https://api.gofile.io/contents/{cid}?wt={token}",
                           headers={"Authorization": f"Bearer {token}"}) as r:
        d = await r.json()
        if d.get("status") != "ok":
            raise ValueError(f"Gofile content : {d.get('status')}")
    children = d["data"].get("children", {})
    if not children:
        raise ValueError("Aucun fichier dans ce lien Gofile.")
    first = next(iter(children.values()))
    if first.get("type") != "file":
        raise ValueError("Le contenu Gofile n'est pas un fichier.")
    return first["link"], os.path.splitext(first.get("name",""))[1].lower()

async def normalize_url(url: str, session: aiohttp.ClientSession):
    if "gofile.io" in url:
        return await resolve_gofile(url, session)
    gd = re.search(r"drive\.google\.com/file/d/([^/?#]+)", url)
    if gd:
        return f"https://drive.google.com/uc?export=download&id={gd.group(1)}", ""
    gs = re.search(r"docs\.google\.com/spreadsheets/d/([^/?#]+)", url)
    if gs:
        return f"https://docs.google.com/spreadsheets/d/{gs.group(1)}/export?format=csv", ".csv"
    if "dropbox.com" in url:
        url = re.sub(r"[?&]dl=0", "", url)
        return url + ("&" if "?" in url else "?") + "dl=1", ""
    pb = re.match(r"https?://pastebin\.com/(?!raw/)([A-Za-z0-9]+)$", url)
    if pb:
        return f"https://pastebin.com/raw/{pb.group(1)}", ".txt"
    gh = re.search(r"github\.com/([^/]+)/([^/]+)/blob/(.+)", url)
    if gh:
        return f"https://raw.githubusercontent.com/{gh.group(1)}/{gh.group(2)}/{gh.group(3)}", ""
    return url, ""

def guess_extension(url: str, ct: str, forced: str) -> str:
    if forced:
        return forced
    ext = os.path.splitext(url.split("?")[0])[1].lower()
    if ext in PARSERS:
        return ext
    return {"text/csv": ".csv","application/csv": ".csv","application/json": ".json",
            "text/plain": ".txt","application/x-ndjson": ".jsonl",
            "application/jsonlines": ".jsonl"}.get(ct.split(";")[0].strip().lower(), "")

def list_local_files():
    return [f for f in os.listdir(LOCAL_DATA_DIR)
            if os.path.isfile(os.path.join(LOCAL_DATA_DIR, f))
            and os.path.splitext(f)[1].lower() in PARSERS] if os.path.isdir(LOCAL_DATA_DIR) else []

def import_local_file(filename: str, source: str = ""):
    fp = os.path.join(LOCAL_DATA_DIR, filename)
    if not os.path.isfile(fp):
        raise FileNotFoundError(fp)
    ext = os.path.splitext(filename)[1].lower()
    if ext not in PARSERS:
        raise ValueError(f"Format non supporté : {ext}")

    # Streaming pour gros JSONL/TXT: évite de charger des fichiers géants en RAM.
    if ext in (".jsonl", ".txt"):
        inserted = 0
        batch = []
        with open(fp, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                if ext == ".jsonl":
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        payload = json.loads(line)
                    except Exception:
                        continue
                    if isinstance(payload, dict):
                        r = extract_row(payload)
                        if r:
                            batch.append(r)
                    elif isinstance(payload, list):
                        for item in payload:
                            if isinstance(item, dict):
                                r = extract_row(item)
                                if r:
                                    batch.append(r)
                else:
                    batch.extend(parse_txt_line(line))

                if len(batch) >= 1000:
                    inserted += insert_batch(batch, source=source or filename)
                    batch.clear()
        if batch:
            inserted += insert_batch(batch, source=source or filename)
        if inserted <= 0:
            raise ValueError("Aucune entrée valide trouvée.")
        return inserted, ext

    with open(fp, "rb") as f:
        data = f.read()
    rows = PARSERS[ext](data)
    if not rows:
        raise ValueError("Aucune entrée valide trouvée.")
    return insert_batch(rows, source=source or filename), ext

def import_all_local_files():
    """Importe tous les fichiers supportés du dossier local."""
    total_imported = 0
    details = []
    errors = []
    files = list_local_files()
    total_files = len(files)
    for idx, filename in enumerate(files, start=1):
        try:
            n, ext = import_local_file(filename, source=filename)
            total_imported += n
            details.append((filename, ext, n))
        except Exception as e:
            errors.append((filename, str(e)))
        if idx % 10 == 0 or idx == total_files:
            print(f"📥 Import local: {idx}/{total_files} fichiers traités | +{total_imported} entrées")
    return total_imported, details, errors

# ══════════════════════════════════════════════════════════
#  BOT
# ══════════════════════════════════════════════════════════
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)
IMPORT_LOCAL_RUNNING = False
IMPORT_LOCAL_TOTAL_FILES = 0
IMPORT_LOCAL_DONE_FILES = 0
IMPORT_LOCAL_TOTAL_ROWS = 0
IMPORT_LOCAL_LAST_ERROR = ""
IMPORT_LOCAL_ACTIVE_FILE = ""

def is_owner(uid: int) -> bool:
    return uid in OWNER_IDS

@bot.event
async def on_ready():
    global IMPORT_LOCAL_RUNNING
    total = count_db()
    local_files_count = len(list_local_files())
    print(f"✅ Connecté : {bot.user}  |  {total} entrées en DB")
    print(f"📁 Dossier local : {os.path.abspath(LOCAL_DATA_DIR)}")
    if total == 0 and local_files_count > 0:
        print(f"ℹ️ {local_files_count} fichier(s) trouvé(s) dans data/, mais pas encore importé(s).")
        print("ℹ️ Utilise /importer_tout_local ou /importer_local <fichier>.")
        if AUTO_IMPORT_ON_START and not IMPORT_LOCAL_RUNNING:
            IMPORT_LOCAL_RUNNING = True
            print("🚀 Auto-import au démarrage lancé (DB vide).")
            channel = bot.get_channel(IMPORT_CHANNEL_ID)
            if channel:
                asyncio.create_task(run_mass_local_import(channel, "auto-start"))
            else:
                # Fallback: exécute quand même sans message Discord si le salon est introuvable.
                asyncio.create_task(run_mass_local_import(None, "auto-start"))
    guild = discord.Object(id=GUILD_ID)
    bot.tree.copy_global_to(guild=guild)
    synced = await bot.tree.sync(guild=guild)
    print(f"✅ {len(synced)} slash commands synced.")

@bot.command()
async def sync(ctx):
    if not is_owner(ctx.author.id):
        await ctx.send("🚫 Permission refusée.")
        return
    guild = discord.Object(id=GUILD_ID)
    bot.tree.copy_global_to(guild=guild)
    synced = await bot.tree.sync(guild=guild)
    await ctx.send(f"✅ {len(synced)} commandes synced.")

@bot.tree.command(name="aide", description="Affiche le menu des commandes du bot")
async def aide(interaction: discord.Interaction):
    embed = styled_embed("📘 Menu des commandes", "purple", "Commandes principales du bot.")
    embed.add_field(
        name="🔎 Recherche",
        value="`/chercher` recherche rapide\n`/recherche` filtres avancés\n`/fiche` fiche par ID",
        inline=False
    )
    embed.add_field(
        name="📥 Import",
        value="`/fichiers_locaux` voir fichiers\n`/importer_local` importer 1 fichier\n`/importer_tout_local` importer tout\n`/importer` importer pièce jointe\n`/importer_url` importer depuis URL",
        inline=False
    )
    embed.add_field(
        name="🛠️ Gestion",
        value="`/ajouter` ajout manuel (owner)\n`/supprimer` suppression par ID (owner)\n`/stats` statistiques DB",
        inline=False
    )
    embed.set_footer(text=f"Total actuel en base: {count_db()} entrée(s)")
    await interaction.response.send_message(embed=embed, ephemeral=True)

# ══════════════════════════════════════════════════════════
#  PAGINATION VIEW
# ══════════════════════════════════════════════════════════
class PaginationView(discord.ui.View):
    def __init__(self, rows: list, query_desc: str = ""):
        super().__init__(timeout=120)
        self.rows       = rows
        self.query_desc = query_desc
        self.index      = 0
        self.mode       = "fiche"   # "fiche" ou "liste"

    def current_embed(self):
        if self.mode == "liste":
            return build_summary_embed(self.rows, self.query_desc)
        return build_person_embed(self.rows[self.index], self.index + 1, len(self.rows))

    @discord.ui.button(emoji="◀️", style=discord.ButtonStyle.secondary, row=0)
    async def prev(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.mode  = "fiche"
        self.index = (self.index - 1) % len(self.rows)
        await interaction.response.edit_message(embed=self.current_embed(), view=self)

    @discord.ui.button(emoji="▶️", style=discord.ButtonStyle.secondary, row=0)
    async def next(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.mode  = "fiche"
        self.index = (self.index + 1) % len(self.rows)
        await interaction.response.edit_message(embed=self.current_embed(), view=self)

    @discord.ui.button(label="📋 Liste complète", style=discord.ButtonStyle.primary, row=0)
    async def toggle_liste(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.mode = "liste" if self.mode == "fiche" else "fiche"
        await interaction.response.edit_message(embed=self.current_embed(), view=self)

# ══════════════════════════════════════════════════════════
#  /chercher  — recherche rapide globale
# ══════════════════════════════════════════════════════════
@bot.tree.command(name="chercher", description="Recherche rapide dans tous les champs (nom, tél, email, ville…)")
@app_commands.describe(query="Ce que tu cherches : nom, prénom, téléphone, email, ville, IP…")
async def chercher(interaction: discord.Interaction, query: str):
    await interaction.response.defer(ephemeral=True)
    rows = search_advanced(query_global=query)
    if not rows:
        embed = discord.Embed(title="❌ Aucun résultat",
                              description=f"Aucune entrée ne correspond à **{query}**.",
                              color=COLORS["red"])
        await interaction.followup.send(embed=embed)
        return
    if len(rows) == 1:
        await interaction.followup.send(embed=build_person_embed(rows[0], 1, 1))
    else:
        view = PaginationView(rows, f"« {query} »")
        await interaction.followup.send(embed=build_summary_embed(rows, f"« {query} »"), view=view)

# ══════════════════════════════════════════════════════════
#  /recherche  — recherche avancée multi-critères
# ══════════════════════════════════════════════════════════
@bot.tree.command(name="recherche", description="Recherche avancée : combiner prénom + nom + téléphone + email + ville…")
@app_commands.describe(
    prenom      = "Filtrer par prénom",
    nom         = "Filtrer par nom",
    telephone   = "Filtrer par numéro de téléphone",
    email       = "Filtrer par email",
    ville       = "Filtrer par ville",
    code_postal = "Filtrer par code postal",
    ip          = "Filtrer par adresse IP",
)
async def recherche(
    interaction: discord.Interaction,
    prenom:      str = "",
    nom:         str = "",
    telephone:   str = "",
    email:       str = "",
    ville:       str = "",
    code_postal: str = "",
    ip:          str = "",
):
    await interaction.response.defer(ephemeral=True)

    if not any([prenom, nom, telephone, email, ville, code_postal, ip]):
        await interaction.followup.send(embed=discord.Embed(
            title="⚠️ Critère manquant",
            description="Renseigne au moins **un critère** de recherche.",
            color=COLORS["orange"]
        ))
        return

    rows = search_advanced(prenom=prenom, nom=nom, telephone=telephone,
                           email=email, ville=ville, code_postal=code_postal, ip=ip)

    # Construit le descriptif des critères
    crit_parts = []
    if prenom:      crit_parts.append(f"Prénom: **{prenom}**")
    if nom:         crit_parts.append(f"Nom: **{nom}**")
    if telephone:   crit_parts.append(f"Tél: **{telephone}**")
    if email:       crit_parts.append(f"Email: **{email}**")
    if ville:       crit_parts.append(f"Ville: **{ville}**")
    if code_postal: crit_parts.append(f"CP: **{code_postal}**")
    if ip:          crit_parts.append(f"IP: **{ip}**")
    crit_str = "  •  ".join(crit_parts)

    if not rows:
        embed = discord.Embed(title="❌ Aucun résultat", color=COLORS["red"])
        embed.add_field(name="🔎 Critères", value=crit_str, inline=False)
        await interaction.followup.send(embed=embed)
        return

    if len(rows) == 1:
        await interaction.followup.send(embed=build_person_embed(rows[0], 1, 1))
        return

    view = PaginationView(rows, crit_str)
    await interaction.followup.send(embed=build_summary_embed(rows, crit_str), view=view)

# ══════════════════════════════════════════════════════════
#  /fiche  — fiche complète par ID
# ══════════════════════════════════════════════════════════
@bot.tree.command(name="fiche", description="Affiche la fiche complète d'une personne via son ID")
@app_commands.describe(id="ID de la personne (visible dans les résultats de /chercher ou /recherche)")
async def fiche(interaction: discord.Interaction, id: int):
    await interaction.response.defer(ephemeral=True)
    r = get_by_id(id)
    if not r:
        await interaction.followup.send(embed=discord.Embed(
            title="❌ ID introuvable",
            description=f"Aucune entrée avec l'ID **#{id}**.",
            color=COLORS["red"]
        ))
        return
    await interaction.followup.send(embed=build_person_embed(r))

# ══════════════════════════════════════════════════════════
#  /ajouter  — ajout manuel complet
# ══════════════════════════════════════════════════════════
@bot.tree.command(name="ajouter", description="[OWNER] Ajoute une personne manuellement avec tous les champs")
@app_commands.describe(
    prenom="Prénom", nom="Nom de famille",
    telephone="Téléphone / mobile", email="Adresse email",
    adresse="Adresse postale", ville="Ville",
    code_postal="Code postal", pays="Pays",
    date_naissance="Date de naissance (ex: 01/01/1990)",
    sexe="Sexe (H / F / Autre)", ip="Adresse IP", notes="Notes libres"
)
async def ajouter(
    interaction: discord.Interaction,
    prenom: str = "", nom: str = "",
    telephone: str = "", email: str = "",
    adresse: str = "", ville: str = "",
    code_postal: str = "", pays: str = "",
    date_naissance: str = "", sexe: str = "",
    ip: str = "", notes: str = "",
):
    if not is_owner(interaction.user.id):
        await interaction.response.send_message("🚫 Permission refusée.", ephemeral=True)
        return
    if not prenom and not nom and not telephone and not email:
        await interaction.response.send_message(
            "❌ Renseigne au moins un prénom, nom, téléphone ou email.", ephemeral=True
        )
        return
    row = {"prenom": prenom, "nom": nom, "telephone": telephone, "email": email,
           "adresse": adresse, "ville": ville, "code_postal": code_postal,
           "pays": pays, "date_naissance": date_naissance, "sexe": sexe,
           "ip": ip, "notes": notes}
    insert_batch([row], source="manuel")
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    r = conn.execute("SELECT * FROM personnes ORDER BY id DESC LIMIT 1").fetchone()
    conn.close()
    await interaction.response.send_message(embed=build_person_embed(dict(r)), ephemeral=True)

# ══════════════════════════════════════════════════════════
#  /supprimer
# ══════════════════════════════════════════════════════════
@bot.tree.command(name="supprimer", description="[OWNER] Supprime une entrée par son ID")
@app_commands.describe(id="ID de l'entrée à supprimer")
async def supprimer(interaction: discord.Interaction, id: int):
    if not is_owner(interaction.user.id):
        await interaction.response.send_message("🚫 Permission refusée.", ephemeral=True)
        return
    r = get_by_id(id)
    if not r:
        await interaction.response.send_message(embed=discord.Embed(
            title="❌ ID introuvable", description=f"Aucune entrée `#{id}`.", color=COLORS["red"]
        ), ephemeral=True)
        return
    nom_aff = f"{r.get('prenom','')} {r.get('nom','')}".strip() or f"entrée #{id}"
    delete_person(id)
    await interaction.response.send_message(embed=discord.Embed(
        title="🗑️ Supprimé",
        description=f"**{nom_aff}** (ID `#{id}`) a été supprimée de la DB.",
        color=COLORS["red"]
    ), ephemeral=True)

# ══════════════════════════════════════════════════════════
#  /stats
# ══════════════════════════════════════════════════════════
@bot.tree.command(name="stats", description="Statistiques détaillées de la base de données")
async def stats(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    conn = sqlite3.connect(DB_PATH)
    total   = conn.execute("SELECT COUNT(*) FROM personnes").fetchone()[0]
    w_tel   = conn.execute("SELECT COUNT(*) FROM personnes WHERE telephone IS NOT NULL AND telephone!=''").fetchone()[0]
    w_email = conn.execute("SELECT COUNT(*) FROM personnes WHERE email IS NOT NULL AND email!=''").fetchone()[0]
    w_addr  = conn.execute("SELECT COUNT(*) FROM personnes WHERE adresse IS NOT NULL AND adresse!=''").fetchone()[0]
    w_ville = conn.execute("SELECT COUNT(*) FROM personnes WHERE ville IS NOT NULL AND ville!=''").fetchone()[0]
    w_ip    = conn.execute("SELECT COUNT(*) FROM personnes WHERE ip IS NOT NULL AND ip!=''").fetchone()[0]
    w_dob   = conn.execute("SELECT COUNT(*) FROM personnes WHERE date_naissance IS NOT NULL AND date_naissance!=''").fetchone()[0]
    conn.close()
    embed = discord.Embed(title="📊 Statistiques de la DB", color=COLORS["purple"])
    embed.add_field(name="👥 Total entrées",   value=f"**{total}**",   inline=True)
    embed.add_field(name="📞 Avec téléphone",  value=f"**{w_tel}**",   inline=True)
    embed.add_field(name="✉️ Avec email",      value=f"**{w_email}**", inline=True)
    embed.add_field(name="🏠 Avec adresse",    value=f"**{w_addr}**",  inline=True)
    embed.add_field(name="📍 Avec ville",      value=f"**{w_ville}**", inline=True)
    embed.add_field(name="🖥️ Avec IP",         value=f"**{w_ip}**",   inline=True)
    embed.add_field(name="🎂 Avec naissance",  value=f"**{w_dob}**",  inline=True)
    embed.set_footer(text=f"DB : {os.path.abspath(DB_PATH)}")
    await interaction.followup.send(embed=embed)

# ══════════════════════════════════════════════════════════
#  IMPORT LOCAL
# ══════════════════════════════════════════════════════════
@bot.tree.command(name="fichiers_locaux", description="[OWNER] Liste les fichiers disponibles dans le dossier local")
async def fichiers_locaux(interaction: discord.Interaction):
    if not is_owner(interaction.user.id):
        await interaction.response.send_message("🚫 Permission refusée.", ephemeral=True)
        return
    fichiers = list_local_files()
    if not fichiers:
        await interaction.response.send_message(embed=discord.Embed(
            title="📂 Dossier vide",
            description=f"Aucun fichier supporté dans `{os.path.abspath(LOCAL_DATA_DIR)}`.\nFormats : {', '.join(PARSERS.keys())}",
            color=COLORS["orange"]
        ), ephemeral=True)
        return
    embed = discord.Embed(title=f"📁 Fichiers dans `{LOCAL_DATA_DIR}/`", color=COLORS["orange"])
    embed.description = "\n".join([f"📄 `{f}`" for f in fichiers])
    embed.set_footer(text=os.path.abspath(LOCAL_DATA_DIR))
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="importer_local", description="[OWNER] Importe un fichier depuis le dossier local du PC")
@app_commands.describe(fichier="Nom du fichier (ex: liste.csv) — /fichiers_locaux pour voir la liste")
async def importer_local(interaction: discord.Interaction, fichier: str):
    if not is_owner(interaction.user.id):
        await interaction.response.send_message("🚫 Permission refusée.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=False)
    try:
        n, ext = import_local_file(fichier)
    except FileNotFoundError:
        dispo = "\n".join([f"• `{f}`" for f in list_local_files()]) or "_(aucun fichier)_"
        await interaction.followup.send(embed=discord.Embed(
            title="❌ Fichier introuvable",
            description=f"`{fichier}` non trouvé dans `{os.path.abspath(LOCAL_DATA_DIR)}`\n\n**Disponibles :**\n{dispo}",
            color=COLORS["red"]
        ))
        return
    except ValueError as e:
        await interaction.followup.send(embed=discord.Embed(title="❌ Erreur", description=str(e), color=COLORS["red"]))
        return
    embed = discord.Embed(title="✅ Import local réussi", color=COLORS["green"])
    embed.add_field(name="📄 Fichier",  value=fichier,        inline=True)
    embed.add_field(name="📂 Format",   value=ext,            inline=True)
    embed.add_field(name="➕ Importés", value=str(n),          inline=True)
    embed.add_field(name="📊 Total DB", value=str(count_db()), inline=False)
    embed.set_footer(text=f"Importé par {interaction.user}")
    await interaction.followup.send(embed=embed)

async def run_mass_local_import(channel: discord.abc.Messageable | None, user_display: str):
    global IMPORT_LOCAL_RUNNING, IMPORT_LOCAL_TOTAL_FILES, IMPORT_LOCAL_DONE_FILES, IMPORT_LOCAL_TOTAL_ROWS, IMPORT_LOCAL_LAST_ERROR, IMPORT_LOCAL_ACTIVE_FILE
    try:
        IMPORT_LOCAL_LAST_ERROR = ""
        files = list_local_files()
        # Commence par les petits fichiers pour afficher de la progression rapidement.
        files = sorted(
            files,
            key=lambda fn: os.path.getsize(os.path.join(LOCAL_DATA_DIR, fn)) if os.path.isfile(os.path.join(LOCAL_DATA_DIR, fn)) else 10**18
        )
        IMPORT_LOCAL_TOTAL_FILES = len(files)
        IMPORT_LOCAL_DONE_FILES = 0
        IMPORT_LOCAL_TOTAL_ROWS = 0
        IMPORT_LOCAL_ACTIVE_FILE = ""
        print(f"🚀 Import en masse démarré: {IMPORT_LOCAL_TOTAL_FILES} fichier(s)")

        def _worker():
            global IMPORT_LOCAL_DONE_FILES, IMPORT_LOCAL_TOTAL_ROWS, IMPORT_LOCAL_ACTIVE_FILE
            total_imported = 0
            details = []
            errors = []
            for idx, filename in enumerate(files, start=1):
                IMPORT_LOCAL_ACTIVE_FILE = filename
                try:
                    fp = os.path.join(LOCAL_DATA_DIR, filename)
                    file_size_mb = os.path.getsize(fp) / (1024 * 1024)
                    if MAX_LOCAL_FILE_SIZE_MB and file_size_mb > MAX_LOCAL_FILE_SIZE_MB:
                        raise ValueError(f"Fichier trop volumineux ({file_size_mb:.1f} MB > {MAX_LOCAL_FILE_SIZE_MB} MB)")
                    n, ext = import_local_file(filename, source=filename)
                    total_imported += n
                    details.append((filename, ext, n))
                except Exception as e:
                    errors.append((filename, str(e)))
                IMPORT_LOCAL_DONE_FILES = idx
                IMPORT_LOCAL_TOTAL_ROWS = total_imported
                if idx % 10 == 0 or idx == len(files):
                    print(f"📥 Progression: {idx}/{len(files)} fichiers | +{total_imported} entrées")
            IMPORT_LOCAL_ACTIVE_FILE = ""
            return total_imported, details, errors

        total_imported, details, errors = await asyncio.to_thread(_worker)
        ok_files = len(details)
        ko_files = len(errors)

        embed = discord.Embed(title="✅ Import local en masse terminé", color=COLORS["green"])
        embed.add_field(name="📄 Fichiers OK", value=str(ok_files), inline=True)
        embed.add_field(name="❌ Fichiers en erreur", value=str(ko_files), inline=True)
        embed.add_field(name="➕ Entrées importées", value=str(total_imported), inline=True)
        embed.add_field(name="📊 Total DB", value=str(count_db()), inline=False)

        if details:
            preview = "\n".join([f"• `{f}` (+{n})" for f, _, n in details[:10]])
            if len(details) > 10:
                preview += f"\n… et {len(details) - 10} autre(s)."
            embed.add_field(name="✅ Aperçu imports", value=preview, inline=False)
        if errors:
            err_preview = "\n".join([f"• `{f}`: {msg[:80]}" for f, msg in errors[:5]])
            if len(errors) > 5:
                err_preview += f"\n… et {len(errors) - 5} autre(s)."
            embed.add_field(name="⚠️ Erreurs", value=err_preview, inline=False)

        embed.set_footer(text=f"Import demandé par {user_display}")
        if channel is not None:
            await channel.send(embed=embed)
        else:
            print(f"✅ Import terminé (sans salon). +{total_imported} entrées, total DB: {count_db()}")
    except Exception as e:
        IMPORT_LOCAL_LAST_ERROR = str(e)
        IMPORT_LOCAL_ACTIVE_FILE = ""
        print(f"❌ Erreur import massif: {e}")
        print(traceback.format_exc())
    finally:
        IMPORT_LOCAL_RUNNING = False

@bot.tree.command(name="importer_tout_local", description="[OWNER] Importe tous les fichiers du dossier local")
async def importer_tout_local(interaction: discord.Interaction):
    global IMPORT_LOCAL_RUNNING, IMPORT_LOCAL_TOTAL_FILES, IMPORT_LOCAL_DONE_FILES, IMPORT_LOCAL_TOTAL_ROWS, IMPORT_LOCAL_LAST_ERROR, IMPORT_LOCAL_ACTIVE_FILE
    if not is_owner(interaction.user.id):
        await interaction.response.send_message("🚫 Permission refusée.", ephemeral=True)
        return
    if IMPORT_LOCAL_RUNNING:
        await interaction.response.send_message("⏳ Un import local massif est déjà en cours.", ephemeral=True)
        return

    fichiers = list_local_files()
    if not fichiers:
        await interaction.response.send_message(embed=discord.Embed(
            title="📂 Dossier vide",
            description=f"Aucun fichier supporté dans `{os.path.abspath(LOCAL_DATA_DIR)}`.",
            color=COLORS["orange"]
        ), ephemeral=True)
        return

    IMPORT_LOCAL_RUNNING = True
    IMPORT_LOCAL_TOTAL_FILES = len(fichiers)
    IMPORT_LOCAL_DONE_FILES = 0
    IMPORT_LOCAL_TOTAL_ROWS = 0
    IMPORT_LOCAL_LAST_ERROR = ""
    IMPORT_LOCAL_ACTIVE_FILE = ""
    await interaction.response.send_message(
        f"🚀 Import lancé en arrière-plan pour **{len(fichiers)}** fichier(s). "
        "Je poste le résultat final ici quand c'est terminé.",
        ephemeral=False
    )
    asyncio.create_task(run_mass_local_import(interaction.channel, str(interaction.user)))

@bot.tree.command(name="etat_import", description="Affiche l'état de l'import local en masse")
async def etat_import(interaction: discord.Interaction):
    if not is_owner(interaction.user.id):
        await interaction.response.send_message("🚫 Permission refusée.", ephemeral=True)
        return
    if IMPORT_LOCAL_RUNNING:
        pct = 0
        if IMPORT_LOCAL_TOTAL_FILES > 0:
            pct = int((IMPORT_LOCAL_DONE_FILES / IMPORT_LOCAL_TOTAL_FILES) * 100)
        embed = styled_embed("⏳ Import en cours", "orange")
        embed.add_field(name="📄 Fichiers traités", value=f"{IMPORT_LOCAL_DONE_FILES}/{IMPORT_LOCAL_TOTAL_FILES} ({pct}%)", inline=False)
        if IMPORT_LOCAL_ACTIVE_FILE:
            embed.add_field(name="📌 Fichier en cours", value=f"`{IMPORT_LOCAL_ACTIVE_FILE}`", inline=False)
        embed.add_field(name="➕ Entrées importées", value=str(IMPORT_LOCAL_TOTAL_ROWS), inline=True)
        embed.add_field(name="📊 Total DB actuel", value=str(count_db()), inline=True)
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return

    embed = styled_embed("✅ Aucun import en cours", "green")
    embed.add_field(name="📊 Total DB", value=str(count_db()), inline=False)
    if IMPORT_LOCAL_LAST_ERROR:
        embed.color = COLORS["red"]
        embed.title = "❌ Dernier import en erreur"
        embed.add_field(name="Erreur", value=IMPORT_LOCAL_LAST_ERROR[:900], inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)

# ══════════════════════════════════════════════════════════
#  IMPORT ATTACHMENT DISCORD
# ══════════════════════════════════════════════════════════
@bot.tree.command(name="importer", description="[OWNER] Importe un fichier joint (pièce jointe Discord)")
@app_commands.describe(fichier="Fichier .csv / .jsonl / .json / .txt")
async def importer(interaction: discord.Interaction, fichier: discord.Attachment):
    if not is_owner(interaction.user.id):
        await interaction.response.send_message("🚫 Permission refusée.", ephemeral=True)
        return
    if interaction.channel_id != IMPORT_CHANNEL_ID:
        ch = bot.get_channel(IMPORT_CHANNEL_ID)
        await interaction.response.send_message(
            f"🚫 Utilise cette commande dans {ch.mention if ch else f'<#{IMPORT_CHANNEL_ID}>'}.", ephemeral=True
        )
        return
    ext = os.path.splitext(fichier.filename)[1].lower()
    if ext not in PARSERS:
        await interaction.response.send_message(
            f"❌ Format `{ext}` non supporté. Acceptés : {', '.join(PARSERS.keys())}", ephemeral=True
        )
        return
    await interaction.response.defer(ephemeral=False)
    try:
        rows = PARSERS[ext](await fichier.read())
    except Exception as e:
        await interaction.followup.send(f"❌ Erreur de lecture : `{e}`")
        return
    if not rows:
        await interaction.followup.send("⚠️ Aucune entrée trouvée dans le fichier.")
        return
    n = insert_batch(rows, source=fichier.filename)
    embed = discord.Embed(title="✅ Import réussi", color=COLORS["green"])
    embed.add_field(name="📄 Fichier",  value=fichier.filename, inline=True)
    embed.add_field(name="➕ Importés", value=str(n),            inline=True)
    embed.add_field(name="📊 Total DB", value=str(count_db()),   inline=True)
    embed.set_footer(text=f"Importé par {interaction.user}")
    await interaction.followup.send(embed=embed)

# ══════════════════════════════════════════════════════════
#  IMPORT URL
# ══════════════════════════════════════════════════════════
@bot.tree.command(name="importer_url", description="[OWNER] Importe depuis une URL (Google Drive, Dropbox…)")
@app_commands.describe(url="Lien direct ou de partage", format="Format si non auto-détectable")
@app_commands.choices(format=[
    app_commands.Choice(name="Auto-détection", value="auto"),
    app_commands.Choice(name="CSV",  value=".csv"),
    app_commands.Choice(name="JSON", value=".json"),
    app_commands.Choice(name="JSONL",value=".jsonl"),
    app_commands.Choice(name="TXT",  value=".txt"),
])
async def importer_url(interaction: discord.Interaction, url: str, format: str = "auto"):
    if not is_owner(interaction.user.id):
        await interaction.response.send_message("🚫 Permission refusée.", ephemeral=True)
        return
    if interaction.channel_id != IMPORT_CHANNEL_ID:
        ch = bot.get_channel(IMPORT_CHANNEL_ID)
        await interaction.response.send_message(
            f"🚫 Utilise cette commande dans {ch.mention if ch else f'<#{IMPORT_CHANNEL_ID}>'}.", ephemeral=True
        )
        return
    await interaction.response.defer(ephemeral=False)
    forced = "" if format == "auto" else format
    try:
        async with aiohttp.ClientSession() as session:
            dl_url, url_ext = await normalize_url(url, session)
            if not forced and url_ext:
                forced = url_ext
            async with session.get(dl_url, allow_redirects=True,
                                   timeout=aiohttp.ClientTimeout(total=30),
                                   headers={"User-Agent": "Mozilla/5.0"}) as resp:
                if resp.status != 200:
                    await interaction.followup.send(f"❌ HTTP {resp.status}")
                    return
                ct   = resp.headers.get("Content-Type", "")
                data = await resp.read()
    except Exception as e:
        await interaction.followup.send(f"❌ Erreur réseau : `{e}`")
        return
    ext = guess_extension(dl_url, ct, forced)
    if ext not in PARSERS:
        await interaction.followup.send(
            f"❌ Format non détecté (`{ext or '?'}`). Précise le paramètre `format`."
        )
        return
    try:
        rows = PARSERS[ext](data)
    except Exception as e:
        await interaction.followup.send(f"❌ Erreur de parsing : `{e}`")
        return
    if not rows:
        await interaction.followup.send("⚠️ Aucune entrée trouvée.")
        return
    n = insert_batch(rows, source=url[:100])
    embed = discord.Embed(title="✅ Import URL réussi", color=COLORS["green"])
    embed.add_field(name="🔗 Source",   value=url[:80]+("…" if len(url)>80 else ""), inline=False)
    embed.add_field(name="📄 Format",   value=ext,          inline=True)
    embed.add_field(name="➕ Importés", value=str(n),        inline=True)
    embed.add_field(name="📊 Total DB", value=str(count_db()), inline=True)
    embed.set_footer(text=f"Importé par {interaction.user}")
    await interaction.followup.send(embed=embed)

# ══════════════════════════════════════════════════════════
bot.run(TOKEN)
