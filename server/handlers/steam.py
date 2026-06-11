"""
server/handlers/steam.py — Steam library and local game launcher
"""

import os
import re
import winreg

import server.state as _st


def get_steam_games_list() -> list:
    games = []

    # 1. Local games from C:\\Games
    local_dir = r"C:\Games"
    if os.path.exists(local_dir):
        try:
            for folder_name in os.listdir(local_dir):
                folder_path = os.path.join(local_dir, folder_name)
                if os.path.isdir(folder_path):
                    exe_path = None
                    try:
                        for filename in os.listdir(folder_path):
                            if (filename.lower().endswith(".exe")
                                    and "unity" not in filename.lower()
                                    and "crash" not in filename.lower()):
                                if filename.lower().startswith(folder_name.lower()):
                                    exe_path = os.path.normpath(os.path.join(folder_path, filename))
                                    break
                                if not exe_path:
                                    exe_path = os.path.normpath(os.path.join(folder_path, filename))
                    except Exception:
                        pass
                    if not exe_path:
                        exe_path = os.path.normpath(folder_path)
                    games.append({
                        "appid": f"local_{folder_name}", "name": folder_name,
                        "exe_path": exe_path, "is_local": True,
                    })
        except Exception as e:
            print("[Local Games Parser] Error:", e)

    # 2. Steam library
    try:
        key       = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam")
        steam_path, _ = winreg.QueryValueEx(key, "SteamPath")
        apps_dir  = os.path.join(steam_path, "steamapps")
        if os.path.exists(apps_dir):
            for f in os.listdir(apps_dir):
                if f.startswith("appmanifest_") and f.endswith(".acf"):
                    try:
                        with open(os.path.join(apps_dir, f), "r", encoding="utf-8") as file:
                            content     = file.read()
                            appid_match = re.search(r'"appid"\s+"(\d+)"', content)
                            name_match  = re.search(r'"name"\s+"([^"]+)"', content)
                            if appid_match and name_match:
                                games.append({"appid": appid_match.group(1), "name": name_match.group(1)})
                    except Exception:
                        pass
    except Exception as e:
        print("[Steam Parser] Error:", e)

    games.sort(key=lambda x: x['name'].lower())
    return games


async def handle(websocket, data: dict, loop) -> bool:
    msg_type = data.get("type")

    if msg_type == "get_steam_games":
        games = await loop.run_in_executor(None, get_steam_games_list)
        await websocket.send_json({"type": "steam_games_list", "data": games})
        return True

    elif msg_type == "launch_steam_game":
        appid = data.get("appid", "")
        if appid:
            if appid.startswith("local_"):
                games_list = await loop.run_in_executor(None, get_steam_games_list)
                game_entry = next((g for g in games_list if g.get("appid") == appid), None)
                if game_entry and game_entry.get("exe_path"):
                    try:
                        os.startfile(game_entry["exe_path"])
                    except Exception as e:
                        print(f"[Local Launcher] Error launching local game: {e}")
            else:
                os.startfile(f"steam://rungameid/{appid}")
        return True

    return False
