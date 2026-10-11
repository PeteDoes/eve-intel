import os
import json
import asyncio
from collections import Counter
from datetime import datetime, timezone
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
import httpx
import redis.asyncio as redis

app = FastAPI()

redis_client = redis.from_url(os.environ.get("REDIS_URL"), decode_responses=True)

HEADERS = {"User-Agent": "eve-intel-app (contact: your-email@example.com)"}

@app.get("/")
def read_root():
    return FileResponse("static/index.html")

def extract_top_list(top_lists, list_type, limit=5):
    for entry in top_lists:
        if entry.get("type") == list_type:
            values = entry.get("values", [])
            return values[:limit]
    return []

async def resolve_names(client, ids):
    """Turn a list of EVE IDs (characters, ships, etc.) into a {id: name} lookup."""
    unique_ids = list(set(i for i in ids if i))
    mapping = {}
    for start in range(0, len(unique_ids), 500):
        chunk = unique_ids[start:start + 500]
        try:
            resp = await client.post(
                "https://esi.evetech.net/latest/universe/names/",
                json=chunk
            )
            data = resp.json()
            if isinstance(data, list):
                for entry in data:
                    if "id" in entry and "name" in entry:
                        mapping[entry["id"]] = entry["name"]
        except Exception:
            pass
    return mapping

async def get_employment_history(client, character_id):
    """Get the character's full corporation history with dates."""
    try:
        history_resp = await client.get(
            f"https://esi.evetech.net/latest/characters/{character_id}/corporationhistory/"
        )
        history_data = history_resp.json()
    except Exception:
        return []

    if not isinstance(history_data, list):
        return []

    corp_ids = list(set(entry.get("corporation_id") for entry in history_data if entry.get("corporation_id")))
    corp_id_to_name = await resolve_names(client, corp_ids)

    history_data.sort(key=lambda entry: entry.get("start_date", ""), reverse=True)

    result = []
    for entry in history_data:
        corp_id = entry.get("corporation_id")
        result.append({
            "corporation_name": corp_id_to_name.get(corp_id, f"Unknown ({corp_id})"),
            "start_date": entry.get("start_date"),
            "is_deleted": entry.get("is_deleted", False),
        })

    return result

def analyze_corp_hopping(history):
    """Work out how long they stayed in each corp, and flag frequent short stints."""
    if not history:
        return [], {"short_stint_count": 0, "total_corps": 0, "is_flagged": False, "reason": "No employment history available"}

    enriched = []
    now = datetime.now(timezone.utc)

    for i, entry in enumerate(history):
        start_str = entry.get("start_date")
        try:
            start_dt = datetime.fromisoformat(start_str.replace("Z", "+00:00")) if start_str else None
        except Exception:
            start_dt = None

        if i == 0:
            end_dt = now
            end_label = "Present"
        else:
            prev_start_str = history[i - 1].get("start_date")
            try:
                end_dt = datetime.fromisoformat(prev_start_str.replace("Z", "+00:00")) if prev_start_str else None
            except Exception:
                end_dt = None
            end_label = end_dt.strftime("%Y-%m-%d") if end_dt else "Unknown"

        duration_days = None
        if start_dt and end_dt:
            duration_days = (end_dt - start_dt).days

        enriched.append({
            "corporation_name": entry.get("corporation_name"),
            "start_date": entry.get("start_date"),
            "end_date": end_label,
            "duration_days": duration_days,
            "is_deleted": entry.get("is_deleted", False),
        })

    short_stints = [e for e in enriched if e["duration_days"] is not None and e["duration_days"] < 30]
    total_corps = len(enriched)
    short_stint_count = len(short_stints)

    is_flagged = short_stint_count >= 3 or (total_corps >= 4 and short_stint_count / total_corps >= 0.5)

    if is_flagged:
        reason = f"{short_stint_count} of {total_corps} corp memberships lasted under 30 days. Frequent short stints can indicate scouting or spying behavior."
    elif short_stint_count > 0:
        reason = f"{short_stint_count} of {total_corps} corp memberships lasted under 30 days. Worth a closer look, but not necessarily alarming on its own."
    else:
        reason = "No unusually short corp memberships detected."

    summary = {
        "short_stint_count": short_stint_count,
        "total_corps": total_corps,
        "is_flagged": is_flagged,
        "reason": reason,
    }

    return enriched, summary

async def get_fleetmates(client, character_id, kill_limit=25, name_limit=15):
    """Scan recent killmails to find who this character actually flies with, plus their corp."""
    try:
        kills_resp = await client.get(
            f"https://zkillboard.com/api/kills/characterID/{character_id}/"
        )
        kills_list = kills_resp.json()
    except Exception:
        return []

    if not isinstance(kills_list, list):
        return []

    kills_to_check = kills_list[:kill_limit]

    fleetmate_counter = Counter()
    fleetmate_corp_id = {}

    for kill in kills_to_check:
        killmail_id = kill.get("killmail_id")
        kill_hash = kill.get("zkb", {}).get("hash")

        if not killmail_id or not kill_hash:
            continue

        try:
            killmail_resp = await client.get(
                f"https://esi.evetech.net/latest/killmails/{killmail_id}/{kill_hash}/"
            )
            killmail_data = killmail_resp.json()
        except Exception:
            continue

        attackers = killmail_data.get("attackers", [])
        for attacker in attackers:
            attacker_id = attacker.get("character_id")
            if attacker_id and attacker_id != character_id:
                fleetmate_counter[attacker_id] += 1
                corp_id = attacker.get("corporation_id")
                if corp_id:
                    fleetmate_corp_id[attacker_id] = corp_id

    if not fleetmate_counter:
        return []

    top_fleetmate_ids = [char_id for char_id, count in fleetmate_counter.most_common(name_limit)]
    id_to_name = await resolve_names(client, top_fleetmate_ids)
    corp_id_to_name = await resolve_names(client, list(set(fleetmate_corp_id.values())))

    result = []
    for char_id, count in fleetmate_counter.most_common(name_limit):
        corp_id = fleetmate_corp_id.get(char_id)
        result.append({
            "name": id_to_name.get(char_id, f"Unknown ({char_id})"),
            "character_id": char_id,
            "corporation_name": corp_id_to_name.get(corp_id, "Unknown"),
            "kills": count
        })

    return result

@app.get("/api/character/{name}")
async def get_character(name: str):
    cache_key = f"character:{name.lower()}"

    cached = await redis_client.get(cache_key)
    if cached:
        result = json.loads(cached)
        result["from_cache"] = True
        return result

    async with httpx.AsyncClient(headers=HEADERS, follow_redirects=True, timeout=30.0) as client:
        search_resp = await client.post(
            "https://esi.evetech.net/latest/universe/ids/",
            json=[name]
        )
        search_data = search_resp.json()

        if "characters" not in search_data or len(search_data["characters"]) == 0:
            return {"error": f"No character found with name '{name}'"}

        character_id = search_data["characters"][0]["id"]

        detail_resp = await client.get(
            f"https://esi.evetech.net/latest/characters/{character_id}/"
        )
        details = detail_resp.json()

        corporation_id = details.get("corporation_id")
        alliance_id = details.get("alliance_id")

        corp_name = None
        if corporation_id:
            corp_resp = await client.get(
                f"https://esi.evetech.net/latest/corporations/{corporation_id}/"
            )
            corp_name = corp_resp.json().get("name")

        alliance_name = None
        if alliance_id:
            alliance_resp = await client.get(
                f"https://esi.evetech.net/latest/alliances/{alliance_id}/"
            )
            alliance_name = alliance_resp.json().get("name")

        zkill_stats = {}
        top_ships = []
        top_corporations = []
        top_alliances = []

        try:
            zkill_resp = await client.get(
                f"https://zkillboard.com/api/stats/characterID/{character_id}/"
            )
            zkill_data = zkill_resp.json()
            top_lists = zkill_data.get("topLists", [])

            zkill_stats = {
                "ships_destroyed": zkill_data.get("shipsDestroyed"),
                "ships_lost": zkill_data.get("shipsLost"),
                "isk_destroyed": zkill_data.get("iskDestroyed"),
                "isk_lost": zkill_data.get("iskLost"),
                "danger_ratio": zkill_data.get("dangerRatio"),
                "gang_ratio": zkill_data.get("gangRatio"),
            }

            for ship in extract_top_list(top_lists, "shipType"):
                top_ships.append({
                    "name": ship.get("shipName"),
                    "kills": ship.get("kills"),
                })

            for corp in extract_top_list(top_lists, "corporation"):
                top_corporations.append({
                    "name": corp.get("corporationName"),
                    "kills": corp.get("kills"),
                })

            for alliance in extract_top_list(top_lists, "alliance"):
                top_alliances.append({
                    "name": alliance.get("allianceName"),
                    "kills": alliance.get("kills"),
                })

        except Exception as e:
            zkill_stats = {"error": f"Could not fetch zKillboard stats: {str(e)}"}

        top_characters = await get_fleetmates(client, character_id, kill_limit=25, name_limit=15)
        raw_employment_history = await get_employment_history(client, character_id)
        employment_history, corp_hop_analysis = analyze_corp_hopping(raw_employment_history)

        result = {
            "name": name,
            "character_id": character_id,
            "zkillboard_url": f"https://zkillboard.com/character/{character_id}/",
            "security_status": details.get("security_status"),
            "birthday": details.get("birthday"),
            "corporation_id": corporation_id,
            "corporation_name": corp_name,
            "alliance_id": alliance_id,
            "alliance_name": alliance_name,
            "zkillboard": zkill_stats,
            "top_ships": top_ships,
            "flies_with_characters": top_characters,
            "flies_with_corporations": top_corporations,
            "flies_with_alliances": top_alliances,
            "employment_history": employment_history,
            "corp_hop_analysis": corp_hop_analysis,
            "from_cache": False,
        }

        await redis_client.set(cache_key, json.dumps(result), ex=600)

        return result

# ---------------------------------------------------------------
# Corp activity report: who lost ships / got kills since a date
# ---------------------------------------------------------------

MAX_PAGES = 3  # each page is up to 200 killmails, newest first

async def fetch_killmail(client, semaphore, km_ref):
    """Fetch one full killmail from ESI (limited to a few at a time)."""
    killmail_id = km_ref.get("killmail_id")
    kill_hash = km_ref.get("zkb", {}).get("hash")
    if not killmail_id or not kill_hash:
        return None
    async with semaphore:
        try:
            resp = await client.get(
                f"https://esi.evetech.net/latest/killmails/{killmail_id}/{kill_hash}/"
            )
            if resp.status_code != 200:
                return None
            return resp.json()
        except Exception:
            return None

async def collect_killmails(client, semaphore, kind, corp_id, since):
    """
    Pull pages of the corp's newest killmails (kind is 'losses' or 'kills'),
    stopping once we pass the start date.
    Returns (killmails_since_date, capped, error_message).
    """
    collected = []
    capped = False

    for page in range(1, MAX_PAGES + 1):
        try:
            resp = await client.get(
                f"https://zkillboard.com/api/{kind}/corporationID/{corp_id}/page/{page}/"
            )
            refs = resp.json()
        except Exception as e:
            return collected, capped, f"Could not reach zKillboard ({kind}): {str(e)}"

        if isinstance(refs, dict):
            message = refs.get("error") or str(refs)[:200]
            return collected, capped, f"zKillboard said: {message}"

        if not isinstance(refs, list) or len(refs) == 0:
            break

        killmails = await asyncio.gather(*[fetch_killmail(client, semaphore, ref) for ref in refs])
        killmails = [km for km in killmails if km]

        reached_start_date = False
        for km in killmails:
            km_date = km.get("killmail_time", "")[:10]
            if km_date >= since:
                collected.append(km)
            else:
                reached_start_date = True

        if reached_start_date or len(refs) < 200:
            break

        if page == MAX_PAGES:
            capped = True

    return collected, capped, None

@app.get("/api/corp-activity")
async def corp_activity(name: str, since: str):
    # Check the date looks right (YYYY-MM-DD)
    try:
        datetime.strptime(since, "%Y-%m-%d")
    except Exception:
        return {"error": "Please enter a valid start date."}

    cache_key = f"corpactivity2:{name.lower()}:{since}"
    cached = await redis_client.get(cache_key)
    if cached:
        result = json.loads(cached)
        result["from_cache"] = True
        return result

    async with httpx.AsyncClient(headers=HEADERS, follow_redirects=True, timeout=60.0) as client:
        # Step 1: find the corp ID
        search_resp = await client.post(
            "https://esi.evetech.net/latest/universe/ids/",
            json=[name]
        )
        search_data = search_resp.json()

        if "corporations" not in search_data or len(search_data["corporations"]) == 0:
            return {"error": f"No corporation found with name '{name}'"}

        corp_id = search_data["corporations"][0]["id"]
        corp_name = search_data["corporations"][0]["name"]

        # Step 2: collect losses and kills since the start date
        semaphore = asyncio.Semaphore(10)
        (loss_killmails, losses_capped, loss_error), (kill_killmails, kills_capped, kill_error) = await asyncio.gather(
            collect_killmails(client, semaphore, "losses", corp_id, since),
            collect_killmails(client, semaphore, "kills", corp_id, since),
        )

        # If zKillboard errored and we got nothing, show the real reason
        if loss_error and kill_error and not loss_killmails and not kill_killmails:
            return {"error": loss_error}

        pilots = {}

        def get_pilot(char_id):
            if char_id not in pilots:
                pilots[char_id] = {
                    "character_id": char_id,
                    "losses": 0,
                    "kills": 0,
                    "ship_ids": Counter(),
                    "last_loss": "",
                }
            return pilots[char_id]

        losses_counted = 0
        for km in loss_killmails:
            km_date = km.get("killmail_time", "")[:10]
            victim = km.get("victim", {})
            if victim.get("corporation_id") == corp_id and victim.get("character_id"):
                pilot = get_pilot(victim["character_id"])
                pilot["losses"] += 1
                if victim.get("ship_type_id"):
                    pilot["ship_ids"][victim["ship_type_id"]] += 1
                if km_date > pilot["last_loss"]:
                    pilot["last_loss"] = km_date
                losses_counted += 1

        kills_counted = 0
        for km in kill_killmails:
            counted_this_km = False
            for attacker in km.get("attackers", []):
                if attacker.get("corporation_id") == corp_id and attacker.get("character_id"):
                    pilot = get_pilot(attacker["character_id"])
                    pilot["kills"] += 1
                    counted_this_km = True
            if counted_this_km:
                kills_counted += 1

        # Step 3: turn IDs into names
        char_ids = list(pilots.keys())
        all_ship_ids = []
        for pilot in pilots.values():
            all_ship_ids.extend(pilot["ship_ids"].keys())

        char_names, ship_names = await asyncio.gather(
            resolve_names(client, char_ids),
            resolve_names(client, all_ship_ids),
        )

        pilot_list = []
        for char_id, pilot in pilots.items():
            ships_lost = [
                {"name": ship_names.get(ship_id, f"Ship {ship_id}"), "count": count}
                for ship_id, count in pilot["ship_ids"].most_common()
            ]
            pilot_list.append({
                "name": char_names.get(char_id, f"Unknown ({char_id})"),
                "character_id": char_id,
                "zkillboard_url": f"https://zkillboard.com/character/{char_id}/",
                "losses": pilot["losses"],
                "kills": pilot["kills"],
                "ships_lost": ships_lost,
                "last_loss": pilot["last_loss"] or None,
            })

        pilot_list.sort(key=lambda p: (p["losses"], p["kills"]), reverse=True)

        result = {
            "corporation_name": corp_name,
            "corporation_id": corp_id,
            "since": since,
            "losses_counted": losses_counted,
            "kills_counted": kills_counted,
            "losses_capped": losses_capped,
            "kills_capped": kills_capped,
            "pilots": pilot_list,
            "from_cache": False,
        }

        # Only cache clean results, so a temporary error isn't remembered
        if not loss_error and not kill_error:
            await redis_client.set(cache_key, json.dumps(result), ex=600)

        return result

app.mount("/", StaticFiles(directory="static"), name="static")
