"""
selene_server.py — Thin entry point for Selene OS Server
---------------------------------------------------------
Run:   python selene_server.py
Then:  npm start   (or open renderer/index.html for quick testing)

WebSocket: ws://localhost:8766/ws
REST:      http://localhost:8766/
"""

import asyncio
import json
import logging
import os
import pathlib
import time
import uuid
from typing import Any, Dict, List, Optional

import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse

# -- Internal packages ---------------------------------------------------------
from server.config   import SERVER_HOST, SERVER_PORT
from server.state    import get_state, broadcast, clients
from server.startup  import lifespan
import server.state  as _st

# Handler dispatch table
from server.handlers import (
    chat          as _h_chat,
    conversations as _h_conv,
    memory        as _h_mem,
    manifest      as _h_manifest,
    knowledge     as _h_knowledge,
    system        as _h_system,
    steam         as _h_steam,
    youtube       as _h_youtube,
    story         as _h_story,
    misc          as _h_misc,
    notifications as _h_notif,
)

load_dotenv()

logger = logging.getLogger("selene_server")

# -- App -----------------------------------------------------------------------

app = FastAPI(title="Selene OS Server", version="0.3.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# -- YouTube proxy endpoint ----------------------------------------------------

@app.get("/yt-proxy", response_class=HTMLResponse)
async def yt_proxy_endpoint(v: str = ""):
    """
    Serves the YouTube IFrame API wrapper.
    Electron's file:// origin sends no referer — serving from localhost/127.0.0.1
    provides a legitimate HTTP origin and fixes Error 152/153.
    """
    if not v:
        return HTMLResponse(content="Missing ?v=VIDEO_ID", status_code=400)

    html = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="referrer" content="strict-origin-when-cross-origin">
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
html,body{{width:100%;height:100%;background:#000;overflow:hidden}}
#yt-player{{width:100%;height:100%}}
#yt-player iframe{{width:100%!important;height:100%!important;border:none!important}}
</style>
</head>
<body>
<div id="yt-player"></div>
<script>
(function(){{
  var tag = document.createElement('script');
  tag.src = 'https://www.youtube.com/iframe_api';
  document.head.appendChild(tag);
}})();

var player, tickTimer;

function onYouTubeIframeAPIReady() {{
  player = new YT.Player('yt-player', {{
    width: '100%', height: '100%',
    videoId: '{v}',
    playerVars: {{ autoplay: 1, rel: 0, modestbranding: 1, playsinline: 1, enablejsapi: 1 }},
    events: {{
      onReady: function() {{
        window.parent.postMessage({{ event: 'onStateChange', info: -1 }}, '*');
      }},
      onStateChange: function(e) {{
        window.parent.postMessage({{ event: 'onStateChange', info: e.data }}, '*');
        clearInterval(tickTimer);
        if (e.data === 1) {{
          tickTimer = setInterval(function() {{
            if (!player || player.getPlayerState() !== 1) return;
            window.parent.postMessage({{
              event: 'onTimeTick',
              info:  player.getCurrentTime()
            }}, '*');
          }}, 2000);
        }}
      }}
    }}
  }});
}}
</script>
</body>
</html>"""
    return HTMLResponse(
        content=html,
        headers={"Referrer-Policy": "strict-origin-when-cross-origin"},
    )


# -- REST endpoints ------------------------------------------------------------

@app.get("/state")
async def state_endpoint():
    """Quick health + state check."""
    return get_state()


@app.get("/steam/image/{appid}")
async def steam_image_endpoint(appid: str):
    """Serve Steam library cache or C:\\Games cover images, with SVG cartridge fallback."""

    def make_svg_fallback(appid_str: str):
        from fastapi import Response
        name_text = appid_str.replace("local_", "").replace("_", " ").replace("-", " ").title()
        if len(name_text) > 20:
            name_text = name_text[:17] + "..."
        svg_data = f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 300 450" width="100%" height="100%">
<rect width="300" height="450" rx="15" fill="#0c0a21" stroke="#4dd9f7" stroke-width="3" />
<line x1="30" y1="40" x2="270" y2="40" stroke="rgba(77,217,247,0.15)" stroke-width="4" />
<line x1="30" y1="50" x2="270" y2="50" stroke="rgba(77,217,247,0.15)" stroke-width="4" />
<line x1="30" y1="60" x2="270" y2="60" stroke="rgba(77,217,247,0.15)" stroke-width="4" />
<rect x="25" y="80" width="250" height="280" rx="8" fill="#14103c" stroke="rgba(224,64,160,0.4)" stroke-width="2" />
<path d="M 25,180 L 275,180 M 25,280 L 275,280 M 100,80 L 100,360 M 200,80 L 200,360" stroke="rgba(77,217,247,0.06)" stroke-width="1.5" />
<rect x="90" y="150" width="120" height="120" rx="12" fill="rgba(45,212,191,0.08)" stroke="#2dd4bf" stroke-width="2.5" />
<circle cx="150" cy="210" r="35" fill="none" stroke="#fbbf24" stroke-width="2" stroke-dasharray="6,4" />
<circle cx="150" cy="210" r="10" fill="#fbbf24" />
<text x="150" y="325" font-family="'Share Tech Mono', 'Courier New', monospace" font-size="15" fill="#ffffff" font-weight="bold" text-anchor="middle" letter-spacing="1">{name_text.upper()}</text>
<text x="150" y="342" font-family="'Share Tech Mono', 'Courier New', monospace" font-size="8.5" fill="#4dd9f7" text-anchor="middle" letter-spacing="2" opacity="0.8">SEGA SYSTEM CORE</text>
<rect x="40" y="390" width="220" height="36" rx="4" fill="#1e1a4a" stroke="rgba(77,217,247,0.2)" stroke-width="1" />
<text x="150" y="412" font-family="'Share Tech Mono', 'Courier New', monospace" font-size="9" fill="#a5b4fc" text-anchor="middle" letter-spacing="4">* PERSISTENT MEMORY *</text>
</svg>"""
        return Response(content=svg_data, media_type="image/svg+xml")

    if appid.startswith("local_"):
        folder_name = appid.replace("local_", "")
        local_path  = os.path.join(r"C:\Games", folder_name)
        if os.path.exists(local_path):
            try:
                for filename in os.listdir(local_path):
                    if filename.lower().endswith((".jpg", ".jpeg", ".png")):
                        img_path = os.path.normpath(os.path.join(local_path, filename))
                        if os.path.isfile(img_path):
                            return FileResponse(img_path)
            except Exception as e:
                logger.error(f"Error scanning local folder images for {appid}: {e}")
        return make_svg_fallback(appid)

    try:
        import winreg
        key         = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam")
        steam_path, _ = winreg.QueryValueEx(key, "SteamPath")
        cache_dir   = os.path.normpath(os.path.join(steam_path, "appcache", "librarycache", appid))
        if os.path.exists(cache_dir):
            for filename in ["library_600x900.jpg", "header.jpg", "library_header.jpg", "library_hero.jpg"]:
                img_path = os.path.normpath(os.path.join(cache_dir, filename))
                if os.path.isfile(img_path):
                    return FileResponse(img_path)
            files      = os.listdir(cache_dir)
            candidates = [f for f in files if not f.endswith(("_blur.jpg", "logo.png")) and f.lower().endswith((".jpg", ".jpeg", ".png"))]
            if candidates:
                candidates.sort()
                return FileResponse(os.path.normpath(os.path.join(cache_dir, candidates[0])))
            if "logo.png" in files:
                return FileResponse(os.path.normpath(os.path.join(cache_dir, "logo.png")))
            for f in files:
                if f.lower().endswith((".jpg", ".jpeg", ".png")):
                    return FileResponse(os.path.normpath(os.path.join(cache_dir, f)))
    except Exception as e:
        logger.error(f"Error fetching steam image for {appid}: {e}")
    return make_svg_fallback(appid)


@app.get("/sounds/{filename}")
async def sounds_endpoint(filename: str):
    """Serve local UI sounds safely from the sounds/ directory."""
    try:
        from urllib.parse import unquote
        safe_filename = os.path.basename(unquote(filename))
        sound_path    = os.path.join(os.path.dirname(__file__), "sounds", safe_filename)
        if os.path.isfile(sound_path):
            return FileResponse(sound_path)
    except Exception as e:
        logger.error(f"Error fetching sound {filename}: {e}")
    return JSONResponse(status_code=404, content={"error": "Sound not found"})


# -- OpenAI-compatible API (Hermes Agent / tool frameworks) --------------------

@app.get("/v1/models")
async def list_models_openai():
    selene_instance = _st.selene_ref
    model_id = selene_instance.llm_caller.model_name if selene_instance else "selene"
    return JSONResponse({
        "object": "list",
        "data":   [{"id": model_id, "object": "model", "created": 0, "owned_by": "selene-os"}],
    })


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    try:
        body: Dict[str, Any] = await request.json()
    except Exception:
        return JSONResponse(
            {"error": {"message": "Invalid JSON body", "type": "invalid_request_error"}},
            status_code=400,
        )

    messages: List[Dict[str, Any]] = body.get("messages", [])

    # Debug: write first Hermes payload once
    _dbg = pathlib.Path("hermes_message_debug.json")
    if not _dbg.exists():
        try:
            _dbg.write_text(json.dumps(body, indent=2, default=str))
            print(f"[Selene Debug]: Wrote first Hermes payload -> {_dbg.resolve()}")
        except Exception:
            pass

    stream:      bool           = body.get("stream", False)
    tools:       Optional[List] = body.get("tools")
    tool_choice: Any            = body.get("tool_choice", "auto")
    temperature: float          = float(body.get("temperature", 0.7))
    max_tokens:  int            = int(body.get("max_tokens", 4096))

    if not messages:
        return JSONResponse(
            {"error": {"message": "messages array is empty", "type": "invalid_request_error"}},
            status_code=400,
        )

    selene_instance = _st.selene_ref
    if selene_instance is None:
        return JSONResponse(
            {"error": {"message": "Selene is not initialised — server may still be loading.", "type": "server_error"}},
            status_code=503,
        )

    loop = asyncio.get_event_loop()
    try:
        assistant_message = await loop.run_in_executor(
            None,
            lambda: selene_instance.llm_caller.call_with_messages(
                messages=messages, temperature=temperature, max_tokens=max_tokens,
                tools=tools, tool_choice=tool_choice,
            )
        )
    except Exception as exc:
        return JSONResponse(
            {"error": {"message": f"Inference error: {exc}", "type": "server_error"}},
            status_code=500,
        )

    is_tool_call = bool(assistant_message.get("tool_calls"))
    if not is_tool_call:
        user_content = ""
        for msg in reversed(messages):
            if msg.get("role") == "user":
                raw          = msg.get("content", "")
                user_content = (
                    " ".join(p.get("text", "") for p in raw if isinstance(p, dict))
                    if isinstance(raw, list) else raw
                )
                break
        if user_content:
            selene_instance.maybe_extract_memory(user_content, assistant_message.get("content", ""))

    model_id      = selene_instance.llm_caller.model_name
    completion_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
    created_ts    = int(time.time())
    finish_reason = "tool_calls" if is_tool_call else "stop"

    if stream:
        async def sse_generator():
            if is_tool_call:
                chunk = {
                    "id": completion_id, "object": "chat.completion.chunk",
                    "created": created_ts, "model": model_id,
                    "choices": [{"index": 0, "delta": {"role": "assistant", "content": None,
                                  "tool_calls": assistant_message["tool_calls"]},
                                 "finish_reason": "tool_calls"}],
                }
                yield f"data: {json.dumps(chunk)}\n\n"
            else:
                content = assistant_message.get("content", "")
                yield f"data: {json.dumps({'id':completion_id,'object':'chat.completion.chunk','created':created_ts,'model':model_id,'choices':[{'index':0,'delta':{'role':'assistant','content':''},'finish_reason':None}]})}\n\n"
                yield f"data: {json.dumps({'id':completion_id,'object':'chat.completion.chunk','created':created_ts,'model':model_id,'choices':[{'index':0,'delta':{'content':content},'finish_reason':None}]})}\n\n"
                yield f"data: {json.dumps({'id':completion_id,'object':'chat.completion.chunk','created':created_ts,'model':model_id,'choices':[{'index':0,'delta':{},'finish_reason':'stop'}]})}\n\n"
            yield "data: [DONE]\n\n"

        return StreamingResponse(
            sse_generator(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return JSONResponse({
        "id":      completion_id,
        "object":  "chat.completion",
        "created": created_ts,
        "model":   model_id,
        "choices": [{"index": 0, "message": assistant_message, "finish_reason": finish_reason}],
        "usage":   {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    })


# -- WebSocket endpoint --------------------------------------------------------

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    clients.add(websocket)
    print(f"[Selene Server]: UI connected  ({len(clients)} client(s))")

    # Per-session YouTube presence / dormancy state
    yt_state: dict = {
        "awaiting_ghost_reply": False,
        "absence_prompted":     False,
        "dormant":              False,
    }

    await websocket.send_json({"type": "connected", "data": get_state()})
    selene = _st.selene_ref
    if selene:
        await websocket.send_json({"type": "conversations", "data": selene.list_conversations()})

    loop = asyncio.get_event_loop()

    try:
        while True:
            data     = await websocket.receive_json()
            msg_type = data.get("type", "")

            # Dispatch through domain handlers (first match wins)
            if await _h_chat.handle(websocket, data, loop):
                continue
            if await _h_conv.handle(websocket, data, loop):
                continue
            if await _h_mem.handle(websocket, data, loop):
                continue
            if await _h_manifest.handle(websocket, data, loop):
                continue
            if await _h_knowledge.handle(websocket, data, loop):
                continue
            if await _h_system.handle(websocket, data, loop):
                continue
            if await _h_steam.handle(websocket, data, loop):
                continue
            if await _h_youtube.handle(websocket, data, loop, yt_state):
                continue
            if await _h_story.handle(websocket, data, loop):
                continue
            if await _h_notif.handle(websocket, data, loop):
                continue
            if await _h_misc.handle(websocket, data, loop):
                continue

            # Catch-all
            await websocket.send_json({"type": "error", "message": f"Unknown message type: {msg_type}"})

    except WebSocketDisconnect:
        clients.discard(websocket)
        print(f"[Selene Server]: UI disconnected  ({len(clients)} client(s))")
    except Exception as exc:
        clients.discard(websocket)
        print(f"[Selene Server]: Client error — {exc}")


# -- Entry point ---------------------------------------------------------------

if __name__ == "__main__":
    print("+" + "-" * 38 + "+")
    print("|   S E L E N E   O S   S E R V E R    |")
    addr_str   = f"ws://{SERVER_HOST}:{SERVER_PORT}/ws"
    padded_addr = addr_str.center(36)
    print(f"| {padded_addr} |")
    print("+" + "-" * 38 + "+")
    uvicorn.run(app, host=SERVER_HOST, port=SERVER_PORT, log_level="warning")
