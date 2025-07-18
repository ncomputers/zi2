#!/usr/bin/env python3
"""Crowd management system version 10."""

import argparse
import asyncio
import json
import os
import queue
import threading
import time
from datetime import date
from pathlib import Path

import cv2
import torch
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import StreamingResponse, HTMLResponse
from fastapi.templating import Jinja2Templates
from loguru import logger
from ultralytics import YOLO
from deep_sort_realtime.deepsort_tracker import DeepSort
import redis
import uvicorn

# Globals
BASE_DIR = Path(__file__).parent
TEMPLATE_DIR = BASE_DIR / "templates"
lock = threading.Lock()
output_frame = None

config: dict
config_path: str
redis_client: redis.Redis
tracker: "FlowTracker" | None = None
cameras: list


def load_config(path: str, r: redis.Redis) -> dict:
    if os.path.exists(path):
        data = json.load(open(path))
        r.set("config", json.dumps(data))
        return data
    raise FileNotFoundError(path)


def save_config(cfg: dict, path: str, r: redis.Redis) -> None:
    with open(path, "w") as f:
        json.dump(cfg, f, indent=2)
    r.set("config", json.dumps(cfg))


def load_cameras(r: redis.Redis, default_url: str) -> list:
    data = r.get("cameras")
    if data:
        try:
            return json.loads(data)
        except json.JSONDecodeError:
            pass
    cams = [{"id": 1, "name": "Camera1", "url": default_url, "mode": "both"}]
    r.set("cameras", json.dumps(cams))
    return cams


def save_cameras(cams: list, r: redis.Redis) -> None:
    r.set("cameras", json.dumps(cams))


class FlowTracker:
    """Tracks entry and exit counts using YOLOv8 and DeepSORT."""

    def __init__(self, src: str, cfg: dict):
        for k, v in cfg.items():
            setattr(self, k, v)
        self.src = src
        self.device = "cuda:0" if torch.cuda.is_available() else "cpu"
        logger.info(f"Loading model {self.model_path} on {self.device}")
        self.model = YOLO(self.model_path)
        if self.device.startswith("cuda"):
            self.model.model.to(self.device)
            torch.backends.cudnn.benchmark = True
        self.tracker = DeepSort(max_age=5)
        self.frame_queue = queue.Queue(maxsize=10)
        self.tracks = {}
        self.redis = redis.Redis.from_url(self.redis_url)
        self.in_count = int(self.redis.get("in_count") or 0)
        self.out_count = int(self.redis.get("out_count") or 0)
        stored_date = self.redis.get("count_date")
        self.prev_date = (
            date.fromisoformat(stored_date.decode()) if stored_date else date.today()
        )
        self.redis.mset({
            "in_count": self.in_count,
            "out_count": self.out_count,
            "count_date": self.prev_date.isoformat(),
        })
        self.running = True

    def update_cfg(self, cfg: dict):
        for k, v in cfg.items():
            setattr(self, k, v)

    def capture_loop(self):
        while self.running:
            cap = cv2.VideoCapture(self.src)
            if not cap.isOpened():
                logger.error(f"Cannot open stream: {self.src}")
                time.sleep(self.retry_interval)
                continue
            logger.info(f"Stream opened: {self.src}")
            while self.running:
                ret, frame = cap.read()
                if not ret:
                    logger.warning(f"Lost stream, retry in {self.retry_interval}s")
                    break
                if self.frame_queue.full():
                    _ = self.frame_queue.get()
                self.frame_queue.put(frame)
            cap.release()
            time.sleep(self.retry_interval)

    def process_loop(self):
        global output_frame
        idx = 0
        while self.running or not self.frame_queue.empty():
            try:
                frame = self.frame_queue.get(timeout=1)
            except queue.Empty:
                continue
            idx += 1
            if date.today() != self.prev_date:
                self.in_count = 0
                self.out_count = 0
                self.tracks.clear()
                self.prev_date = date.today()
                self.redis.mset({
                    "in_count": self.in_count,
                    "out_count": self.out_count,
                    "count_date": self.prev_date.isoformat(),
                })
                logger.info("Daily counts reset")
            if self.skip_frames and idx % self.skip_frames:
                continue
            res = self.model.predict(frame, device=self.device, verbose=False)[0]
            h, w = frame.shape[:2]
            x_line = int(w * self.line_ratio)
            cv2.line(frame, (x_line, 0), (x_line, h), (255, 0, 0), 2)
            dets = [
                ([int(xyxy[0]), int(xyxy[1]), int(xyxy[2]-xyxy[0]), int(xyxy[3]-xyxy[1])], conf, 'person')
                for *xyxy, conf, cls in res.boxes.data.tolist()
                if int(cls) == 0 and conf >= self.conf_thresh
            ]
            tracks = self.tracker.update_tracks(dets, frame=frame)
            now = time.time()
            for tr in tracks:
                if not tr.is_confirmed():
                    continue
                tid = tr.track_id
                x1, y1, x2, y2 = map(int, tr.to_ltrb())
                cx = (x1 + x2) // 2
                zone = 'left' if cx < x_line else 'right'
                if tid not in self.tracks:
                    self.tracks[tid] = {'zone': zone, 'cx': cx, 'time': now, 'last': None}
                prev = self.tracks[tid]
                if zone != prev['zone'] and abs(cx-prev['cx']) > self.v_thresh and now-prev['time'] > self.debounce:
                    direction = None
                    if prev['zone']=='left' and zone=='right':
                        direction = 'Entering'
                    elif prev['zone']=='right' and zone=='left':
                        direction = 'Exiting'
                    if direction:
                        if prev['last'] is None:
                            if direction=='Entering':
                                self.in_count += 1
                            else:
                                self.out_count += 1
                            self.redis.mset({'in_count': self.in_count, 'out_count': self.out_count})
                            prev['last'] = direction
                            logger.info(f"{direction} ID{tid}: In={self.in_count} Out={self.out_count}")
                        elif prev['last'] != direction:
                            if prev['last']=='Entering':
                                self.in_count -= 1
                            else:
                                self.out_count -= 1
                            self.redis.mset({'in_count': self.in_count, 'out_count': self.out_count})
                            prev['last'] = None
                            logger.info(f"Reversed flow for ID{tid}")
                        prev['time'] = now
                prev['zone'], prev['cx'] = zone, cx
                color = (0,255,0) if zone=='right' else (0,0,255)
                cv2.rectangle(frame, (x1,y1), (x2,y2), color, 2)
                cv2.putText(frame, f"ID{tid}", (x1,y1-10), cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 2)
            cv2.putText(frame, f"Entering: {self.in_count}", (10,30), cv2.FONT_HERSHEY_SIMPLEX, 1, (0,255,0), 2)
            cv2.putText(frame, f"Exiting: {self.out_count}", (10,70), cv2.FONT_HERSHEY_SIMPLEX, 1, (0,0,255), 2)
            with lock:
                output_frame = frame.copy()
            time.sleep(1/self.fps)


app = FastAPI()
templates = Jinja2Templates(directory=str(TEMPLATE_DIR))


@app.get("/")
async def index(request: Request):
    current = tracker.in_count - tracker.out_count
    warn_lim = tracker.max_capacity * tracker.warn_threshold / 100
    status = 'green' if current < warn_lim else 'yellow' if current < tracker.max_capacity else 'red'
    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "max_capacity": tracker.max_capacity,
        "status": status,
        "current": current,
    })


@app.get("/video_feed")
async def video_feed():
    async def gen():
        global output_frame
        while True:
            with lock:
                frame = output_frame
            if frame is None:
                await asyncio.sleep(0.1)
                continue
            _, buf = cv2.imencode('.jpg', frame)
            yield (b'--frame\r\nContent-Type: image/jpeg\r\n\r\n' + buf.tobytes() + b'\r\n')
            await asyncio.sleep(1/tracker.fps)
    return StreamingResponse(gen(), media_type='multipart/x-mixed-replace; boundary=frame')


@app.websocket('/ws/stats')
async def ws_stats(ws: WebSocket):
    await ws.accept()
    try:
        while True:
            in_c = tracker.in_count
            out_c = tracker.out_count
            current = in_c - out_c
            max_cap = tracker.max_capacity
            warn_lim = max_cap * tracker.warn_threshold / 100
            status = 'green' if current < warn_lim else 'yellow' if current < max_cap else 'red'
            await ws.send_json({
                'in_count': in_c,
                'out_count': out_c,
                'current': current,
                'max_capacity': max_cap,
                'status': status,
            })
            await asyncio.sleep(1)
    except WebSocketDisconnect:
        logger.info("WebSocket disconnected")


@app.get('/settings')
async def settings_page(request: Request):
    return templates.TemplateResponse('settings.html', {'request': request, 'cfg': config})


@app.post('/settings')
async def update_settings(request: Request):
    data = await request.json()
    for key in ['max_capacity', 'warn_threshold', 'fps', 'skip_frames', 'line_ratio',
                'v_thresh', 'debounce', 'retry_interval', 'conf_thresh']:
        if key in data:
            val = data[key]
            config[key] = type(getattr(tracker, key))(val)
    save_config(config, config_path, redis_client)
    tracker.update_cfg(config)
    return {"saved": True}


@app.get('/cameras')
async def cameras_page(request: Request):
    return templates.TemplateResponse('cameras.html', {'request': request, 'cams': cameras})


@app.post('/cameras')
async def add_camera(request: Request):
    data = await request.json()
    name = data.get('name') or f"Camera{len(cameras)+1}"
    url = data.get('url')
    mode = data.get('mode', 'both')
    if not url:
        return {'error': 'Missing URL'}
    # test camera
    cap = cv2.VideoCapture(url)
    ok = cap.isOpened()
    if ok:
        cap.release()
    if not ok:
        return {'error': 'Cannot open camera'}
    cam_id = max([c['id'] for c in cameras], default=0) + 1
    cam = {'id': cam_id, 'name': name, 'url': url, 'mode': mode}
    cameras.append(cam)
    save_cameras(cameras, redis_client)
    return {'added': True, 'camera': cam}


def main():
    global config, config_path, redis_client, tracker, cameras
    parser = argparse.ArgumentParser()
    parser.add_argument('stream_url', nargs='?')
    parser.add_argument('-c', '--config', default='config.json')
    parser.add_argument('-w', '--workers', type=int, default=None)
    args = parser.parse_args()

    config_path = args.config if os.path.isabs(args.config) else str(BASE_DIR / args.config)
    temp_cfg = json.load(open(config_path))
    redis_client = redis.Redis.from_url(temp_cfg.get('redis_url', 'redis://localhost:6379/0'))
    config = load_config(config_path, redis_client)
    cameras = load_cameras(redis_client, config['stream_url'])
    url = args.stream_url or cameras[0]['url']

    cores = os.cpu_count() or 1
    workers = args.workers if args.workers is not None else config['default_workers']
    w = max((cores-1 if workers == -1 else (1 if workers == 0 else workers)), 1)
    cv2.setNumThreads(w)
    torch.set_num_threads(w)
    logger.info(f"Threads={w}, cores={cores}")

    tracker = FlowTracker(url, config)
    threading.Thread(target=tracker.capture_loop, daemon=True).start()
    threading.Thread(target=tracker.process_loop, daemon=True).start()

    logger.info(f"Server http://0.0.0.0:{config['port']} Stream={url}")
    uvicorn.run(app, host='0.0.0.0', port=config['port'], log_config=None)


if __name__ == '__main__':
    main()
