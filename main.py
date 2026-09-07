from fastapi import FastAPI
import httpx

app = FastAPI()

@app.get("/")
def read_root():
    return {"status": "EVE Intel backend is running"}

@app.get("/character/{name}")
async def get_character(name: str):
    async with httpx.AsyncClient() as client:
        # Step 1: Convert the character name into an ID
        search_resp = await client.post(
            "https://esi.evetech.net/latest/universe/ids/",
            json=[name]
        )
        search_data = search_resp.json()

        if "characters" not in search_data or len(search_data["characters"]) == 0:
            return {"error": f"No character found with name '{name}'"}

        character_id = search_data["characters"][0]["id"]

        # Step 2: Get full character details using the ID
        detail_resp = await client.get(
            f"https://esi.evetech.net/latest/characters/{character_id}/"
        )
        details = detail_resp.json()

        corporation_id = details.get("corporation_id")
        alliance_id = details.get("alliance_id")

        # Step 3: Get the corporation name
        corp_name = None
        if corporation_id:
            corp_resp = await client.get(
                f"https://esi.evetech.net/latest/corporations/{corporation_id}/"
            )
            corp_data = corp_resp.json()
            corp_name = corp_data.get("name")

        # Step 4: Get the alliance name (if they're in one)
        alliance_name = None
        if alliance_id:
            alliance_resp = await client.get(
                f"https://esi.evetech.net/latest/alliances/{alliance_id}/"
            )
            alliance_data = alliance_resp.json()
            alliance_name = alliance_data.get("name")

        return {
            "name": name,
            "character_id": character_id,
            "security_status": details.get("security_status"),
            "birthday": details.get("birthday"),
            "corporation_id": corporation_id,
            "corporation_name": corp_name,
            "alliance_id": alliance_id,
            "alliance_name": alliance_name,
        }
