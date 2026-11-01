#!/usr/bin/env python3
"""Passat 35i desktop simulator (Pi 5, 4x 28BYJ-48 + ULN2003)."""
import math
import json
from pathlib import Path
import random
import threading
import time
from flask import Flask, jsonify, render_template_string
from gpiozero import OutputDevice

# BCM GPIOs, in IN1/IN2/IN3/IN4 order.
CONFIG = [
    dict(name='Drehzahl', pins=[17, 18, 27, 22], max_deg=250, increasing_direction=1),
    dict(name='Geschwindigkeit', pins=[23, 24, 25, 5], max_deg=250, increasing_direction=1),
    dict(name='Tank', pins=[6, 12, 13, 19], max_deg=80, increasing_direction=-1),
    dict(name='Temperatur', pins=[16, 20, 21, 26], max_deg=80, increasing_direction=1),
]
STEPS_REV = 4076  # approximate, calibrate against your motors
STEP_DELAY = 0.0045  # seconds per half-step; increase for slower movement
SEQ = ((1,0,0,0),(1,1,0,0),(0,1,0,0),(0,1,1,0),
       (0,0,1,0),(0,0,1,1),(0,0,0,1),(1,0,0,1))

# Kilometerstand wird zwischen Neustarts beibehalten.
ODOMETER_FILE = Path.home() / '.passat35i_kilometer.json'
try:
    saved_odometer = max(0.0, float(json.loads(ODOMETER_FILE.read_text()).get('km', 0)))
except (OSError, ValueError, TypeError, AttributeError):
    saved_odometer = 0.0

def save_odometer(km):
    try:
        tmp = ODOMETER_FILE.with_suffix('.tmp')
        tmp.write_text(json.dumps({'km': round(km, 5)}))
        tmp.replace(ODOMETER_FILE)
    except OSError as exc:
        print('Kilometerstand konnte nicht gespeichert werden:', exc)

app = Flask(__name__)
lock = threading.RLock()
stop_threads = threading.Event()
state = dict(mode='nicht_referenziert', phase='Bitte Nullposition bestätigen',
             speed=0.0, rpm=0.0, fuel=0.0, temp=0.0,
             route='Landstraße', ready=False, segment_until=0.0, target_speed=0.0,
             fuel_stop=False, fuel_level=75.0, drive_time=0.0,
             gear=1, shifting_until=0.0, odometer_km=saved_odometer, trip_km=0.0)

class Motor:
    def __init__(self, cfg):
        self.cfg = cfg
        self.pins = [OutputDevice(p, initial_value=False) for p in cfg['pins']]
        self.max_steps = round(cfg['max_deg'] * STEPS_REV / 360)
        self.pos = 0  # software-relative; only valid after physical zero alignment
        self.target = 0
        self.phase = 0
        self.lock = threading.RLock()
        self.worker = threading.Thread(target=self.run, daemon=True)
        self.worker.start()

    def set_angle(self, degrees):
        dest = max(0.0, min(self.cfg['max_deg'], degrees))
        with self.lock:
            self.target = round(dest * STEPS_REV / 360)

    def run(self):
        while not stop_threads.is_set():
            with self.lock:
                diff = self.target - self.pos
                if diff:
                    direction = (1 if diff > 0 else -1)
                    self.phase = (self.phase + direction * self.cfg['increasing_direction']) % 8
                    pattern = SEQ[self.phase]
                    for pin, v in zip(self.pins, pattern):
                        pin.on() if v else pin.off()
                    self.pos += direction
                else:
                    # Motors are de-energized at rest to limit heat.
                    for pin in self.pins:
                        pin.off()
            time.sleep(STEP_DELAY if diff else 0.025)
        for pin in self.pins:
            pin.off()

    def at_zero(self):
        with self.lock:
            return self.pos == 0 and self.target == 0

    def close(self):
        self.worker.join(timeout=2)
        for pin in self.pins:
            pin.close()

motors = [Motor(c) for c in CONFIG]


def apply_targets():
    with lock:
        motors[0].set_angle(state['rpm'] / 7000 * 250)
        motors[1].set_angle(state['speed'] / 240 * 250)
        motors[2].set_angle(state['fuel'] / 100 * 80)
        motors[3].set_angle(state['temp'] / 120 * 80)


def pick_segment(now):
    route = random.choices(['Landstraße', 'Autobahn', 'Stadt'], weights=[5, 3, 2])[0]
    state['route'] = route
    if route == 'Landstraße':
        speed = random.choice([55, 65, 75, 85, 95, 100])
        duration = random.uniform(15, 35)
    elif route == 'Autobahn':
        speed = random.choice([90, 110, 120, 130, 145, 160, 180, 200, 220])
        duration = random.uniform(35, 80)
    else:
        speed = random.choice([0, 20, 35, 45, 50])
        duration = random.uniform(12, 25)
    state['target_speed'] = speed
    state['segment_until'] = now + duration
    state['phase'] = route


def smooth(current, goal, limit):
    return current + max(-limit, min(limit, goal-current))


# Gear ratios in rpm per km/h, deliberately simplified for a desk simulator.
GEAR_RATIOS = {1: 145, 2: 83, 3: 55, 4: 39, 5: 27}
UPSHIFT_AT = {1: 23, 2: 46, 3: 78, 4: 117}
DOWNSHIFT_BELOW = {2: 13, 3: 34, 4: 62, 5: 98}


def update_gear(now, v):
    """Hysteresis prevents hunting near a shift threshold."""
    g = state['gear']
    if now < state['shifting_until']:
        return
    if g < 5 and v > UPSHIFT_AT[g]:
        state['gear'] = g + 1
        state['shifting_until'] = now + 1.5
        state['phase'] = f'Hochschalten in Gang {g + 1}'
    elif g > 1 and v < DOWNSHIFT_BELOW[g]:
        state['gear'] = g - 1
        state['shifting_until'] = now + 1.5
        state['phase'] = f'Zurückschalten in Gang {g - 1}'


def simulator():
    last = time.monotonic()
    last_saved = last
    while not stop_threads.wait(0.1):
        now = time.monotonic()
        dt = min(now-last, 0.5)
        last = now
        with lock:
            mode = state['mode']
            if mode == 'fahrt':
                state['drive_time'] += dt
                distance = state['speed'] * dt / 3600.0
                state['odometer_km'] += distance
                state['trip_km'] += distance
                if state['fuel_level'] <= 25 and not state['fuel_stop']:
                    state['fuel_stop'] = True
                    state['phase'] = 'Tankstelle anfahren'
                if state['fuel_stop']:
                    state['target_speed'] = 0
                elif now >= state['segment_until']:
                    pick_segment(now)
                # Deliberately gentle acceleration/deceleration for slow gauges.
                state['speed'] = smooth(state['speed'], state['target_speed'], 3.5*dt)
                # Wait at pump until needle/speedometer has actually reached zero.
                if state['fuel_stop'] and state['speed'] <= 0.05 and motors[1].at_zero():
                    state['speed'] = 0
                    state['mode'] = 'tanken'
                    state['phase'] = 'An der Zapfsäule'
                state['fuel_level'] = max(0, state['fuel_level'] - (0.13 + state['speed']/1100) * dt)
                v = state['speed']
                # Five-speed automatic with shift pauses and needle-friendly ramp.
                update_gear(now, v)
                g = state['gear']
                rpm_target = 880 if v < 1 else max(900, min(6000, v * GEAR_RATIOS[g]))
                if now < state['shifting_until']:
                    # A noticeable brief dip while the clutch/gear change happens.
                    rpm_target = max(950, rpm_target * 0.82)
                state['rpm'] = smooth(state['rpm'], rpm_target, 850*dt)
                state['temp'] = smooth(state['temp'], 90, 0.7*dt)
                state['fuel'] = state['fuel_level']
            elif mode == 'tanken':
                state['speed'] = 0
                state['rpm'] = smooth(state['rpm'], 0, 450*dt)
                state['fuel_level'] = min(95, state['fuel_level'] + 2.2*dt)
                state['fuel'] = state['fuel_level']
                if state['fuel_level'] >= 95:
                    state['fuel_stop'] = False
                    state['mode'] = 'fahrt'
                    state['segment_until'] = 0
                    state['phase'] = 'Tank voll – Weiterfahrt'
            elif mode == 'rueckfahrt':
                state['speed'] = 0
                state['rpm'] = 0
                state['fuel'] = 0
                state['temp'] = 0
                if all(m.at_zero() for m in motors):
                    state['mode'] = 'bereit'
                    state['phase'] = 'Alle Zeiger auf Null'
            apply_targets()
            if now - last_saved >= 30:
                save_odometer(state['odometer_km'])
                last_saved = now


HTML = '''<!doctype html>
<html lang="de"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="theme-color" content="#080d16"><title>PiTacho | Passat 35i</title>
<style>
:root{color-scheme:dark;--bg:#080d16;--panel:#111d2d;--panel2:#142336;--line:#29374b;--muted:#91a3bc;--text:#f3f7fd;--accent:#54d9ff;--green:#66e3aa;--amber:#f6ba71;--red:#ff7183}*{box-sizing:border-box}body{margin:0;background:radial-gradient(ellipse at 85% 0%,#17304a 0%,transparent 44%),radial-gradient(ellipse at 10% 85%,#0d2834 0%,transparent 45%),var(--bg);font:15px/1.5 system-ui,-apple-system,Segoe UI,sans-serif;color:var(--text);min-height:100vh}main{max-width:1240px;margin:auto;padding:30px 24px 60px}.top{display:flex;justify-content:space-between;align-items:center;gap:18px;flex-wrap:wrap}.brand{display:flex;align-items:center;gap:15px}.logo{width:48px;height:48px;border-radius:15px;display:grid;place-items:center;background:linear-gradient(135deg,#19344f,#132232);border:1px solid #38536d;color:var(--accent);font-size:26px}.eyebrow{font-size:11px;font-weight:800;letter-spacing:.2em;text-transform:uppercase;color:var(--accent)}h1{margin:0;font-weight:750;letter-spacing:-.045em;font-size:29px}h2{margin:0;font-size:17px}.subtitle{color:var(--muted);margin:3px 0 0}.connection{display:flex;align-items:center;gap:9px;border:1px solid var(--line);padding:10px 14px;border-radius:50px;color:#cbd6e4;background:#101a28}.dot{height:9px;width:9px;border-radius:50%;background:var(--amber)}.dot.online{background:var(--green);box-shadow:0 0 14px var(--green)}.dot.online.pulse{animation:pulse 2s infinite}@keyframes pulse{50%{opacity:.35}}.layout{display:grid;grid-template-columns:minmax(0,1fr) 315px;gap:18px;margin-top:27px}.panel{border:1px solid var(--line);border-radius:20px;background:linear-gradient(145deg,rgba(24,38,55,.96),rgba(14,24,38,.96));box-shadow:0 16px 34px #0002}.dash{padding:22px}.panelhead{display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap}.tag{color:var(--green);font-size:12px;font-weight:750;border:1px solid #315449;background:#17372e;padding:5px 11px;border-radius:30px}.gauges{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px;margin-top:20px}.gauge{text-align:center;position:relative;overflow:hidden;background:linear-gradient(155deg,#101c2c,#0b1421);border:1px solid #22364c;border-radius:18px;padding:17px 8px 10px}.gauge .name{font-size:12px;letter-spacing:.1em;text-transform:uppercase;color:#a7b6c9;font-weight:750}.dial{width:100%;max-width:280px;display:block;margin:0 auto -6px;overflow:visible}.track{fill:none;stroke:#2c3a4c;stroke-width:13;stroke-linecap:round}.progress{fill:none;stroke:var(--accent);stroke-width:13;stroke-linecap:round;stroke-dasharray:0 999;filter:drop-shadow(0 0 6px #50caff55)}.tick{stroke:#52637a;stroke-width:2}.needle{stroke:#eff8ff;stroke-width:3.6;stroke-linecap:round;filter:drop-shadow(0 0 6px #8be9ff55);transition:transform .45s ease}.hub{fill:#a4eaff;stroke:#0b1522;stroke-width:4}.digital{position:absolute;bottom:24px;left:0;right:0;font-size:35px;font-weight:800;letter-spacing:-.055em;font-variant-numeric:tabular-nums}.digital small{font-size:12px;font-weight:600;color:var(--muted);letter-spacing:0}.readings{display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-top:17px}.tile{border:1px solid #2a3b51;background:#0d1826;padding:17px;border-radius:15px}.tilehead{display:flex;align-items:center;justify-content:space-between;color:#a0b2c8;font-size:12px}.tile strong{font-size:25px;font-variant-numeric:tabular-nums}.trackbar{height:8px;border-radius:20px;background:#29394a;margin-top:15px;overflow:hidden}.fill{height:100%;width:0;background:linear-gradient(90deg,#258fbc,var(--accent));border-radius:20px;transition:width .8s ease}.fuelbar{background:linear-gradient(90deg,#d38a49,var(--green))}.side{display:flex;flex-direction:column;gap:18px}.controls{padding:22px}.muted{color:var(--muted);font-size:13px}.phase{margin:13px 0 6px;font-size:22px;font-weight:750;line-height:1.2}.route{display:flex;align-items:center;gap:8px;color:#c4d6ec;margin-bottom:18px}.gear{background:linear-gradient(135deg,#163753,#1b2a3b);border:1px solid #32506a;border-radius:15px;padding:13px;display:flex;justify-content:space-between;align-items:center;margin:18px 0}.gear span{font-size:12px;color:var(--muted)}.gear strong{font-size:42px;line-height:1}.buttons{display:grid;gap:10px}.btn{border:1px solid transparent;border-radius:12px;padding:14px 16px;font-size:14px;font-weight:800;cursor:pointer;transition:transform .15s,filter .15s;background:#2c425a;color:white}.btn:not(:disabled):hover{transform:translateY(-2px);filter:brightness(1.1)}.btn:disabled{opacity:.35;cursor:not-allowed}.start{background:var(--green);color:#07231b}.stop{background:#412836;color:#ffb4bd;border-color:#7b414f}.cal{background:#26374b;border-color:#3c536e;color:#d7e9ff}.msg{min-height:20px;margin-top:14px;color:var(--amber);font-size:12px}.distance{padding:22px}.odo{background:#0b121d;border:1px solid #314155;border-radius:12px;padding:14px 12px;text-align:center;margin-top:14px;font-size:32px;font-weight:800;font-family:ui-monospace,SFMono-Regular,Consolas,monospace;letter-spacing:.05em;font-variant-numeric:tabular-nums;text-shadow:0 0 14px #77dfff3a;white-space:nowrap}.odo small{font-size:12px;color:var(--muted);letter-spacing:0}.trip{display:flex;justify-content:space-between;margin-top:13px;color:var(--muted)}.trip b{color:#fff;font-variant-numeric:tabular-nums}.footnote{font-size:12px;color:#8297b1;margin-top:16px}.metrics{margin-top:16px;display:grid;grid-template-columns:repeat(3,1fr);gap:12px}.metric{padding:14px 16px;background:#0e1928;border:1px solid #233349;border-radius:13px}.metric small{display:block;color:var(--muted)}.metric b{font-size:18px;font-variant-numeric:tabular-nums}.statusline{font-size:12px;color:var(--muted);margin-top:17px}@media(max-width:900px){.layout{grid-template-columns:1fr}.side{display:grid;grid-template-columns:1fr 1fr}.gauges{gap:10px}}@media(max-width:580px){main{padding:18px 12px 40px}.dash{padding:13px}.gauges{grid-template-columns:1fr 1fr;gap:7px}.gauge{padding:12px 4px 7px}.digital{font-size:23px;bottom:14px}.digital small{font-size:10px}.readings{gap:8px}.tile{padding:13px}.tile strong{font-size:21px}.side{display:flex}.metrics{grid-template-columns:1fr 1fr}.phase{font-size:19px}.dial{max-width:240px}.logo{width:39px;height:39px}.brand h1{font-size:25px}}
</style></head><body><main>
<header class="top"><div class="brand"><div class="logo">↗</div><div><div class="eyebrow">PiTacho / Virtual Drive</div><h1>Passat 35i <span style="font-weight:350;color:#7f9ab4">Simulator</span></h1><div class="subtitle">Dein Schreibtisch fährt mit.</div></div></div><div class="connection"><i id="dot" class="dot"></i><span id="connection">Verbinde mit Raspberry Pi …</span></div></header>
<div class="layout"><section class="panel dash"><div class="panelhead"><h2>Live-Cockpit</h2><span id="modepill" class="tag">BEREIT</span></div><div class="gauges">
<div class="gauge"><span class="name">Geschwindigkeit</span><svg class="dial" viewBox="0 0 260 210" aria-label="Tacho" role="img"><path class="track" d="M 42 158 A 104 104 0 1 1 218 158"/><path id="speedarc" class="progress" d="M 42 158 A 104 104 0 1 1 218 158"/><g id="speedneedle" class="needle" style="transform-origin:130px 144px"><path d="M130 144 L130 63"/></g><circle class="hub" cx="130" cy="144" r="7"/><text x="36" y="187" fill="#69809b" font-size="13">0</text><text x="192" y="187" fill="#69809b" font-size="13">240</text></svg><div class="digital"><span id="speed">0</span> <small>km/h</small></div></div>
<div class="gauge"><span class="name">Drehzahl</span><svg class="dial" viewBox="0 0 260 210" aria-label="Drehzahlmesser" role="img"><path class="track" d="M 42 158 A 104 104 0 1 1 218 158"/><path id="rpmarc" class="progress" d="M 42 158 A 104 104 0 1 1 218 158"/><g id="rpmneedle" class="needle" style="transform-origin:130px 144px"><path d="M130 144 L130 63"/></g><circle class="hub" cx="130" cy="144" r="7"/><text x="36" y="187" fill="#69809b" font-size="13">0</text><text x="192" y="187" fill="#69809b" font-size="13">7</text></svg><div class="digital"><span id="rpm">0</span> <small>U/min</small></div></div>
</div><div class="readings"><div class="tile"><div class="tilehead"><span>⛽ Tankinhalt</span><span id="fuellabel">0 %</span></div><div style="margin-top:7px"><strong id="fuel">0</strong> <small class="muted">%</small></div><div class="trackbar"><div class="fill fuelbar" id="fuelbar"></div></div></div><div class="tile"><div class="tilehead"><span>♨ Motortemperatur</span><span id="templabel">0 °C</span></div><div style="margin-top:7px"><strong id="temp">0</strong> <small class="muted">°C</small></div><div class="trackbar"><div class="fill" id="tempbar"></div></div></div></div>
<div class="metrics"><div class="metric"><small>Zielgeschwindigkeit</small><b id="target">0 km/h</b></div><div class="metric"><small>Strecke</small><b id="route2">Landstraße</b></div><div class="metric"><small>Fahrzeit</small><b id="driveTime">00:00</b></div></div><div class="statusline">Virtuelle Messwerte · Die echten Zeiger können wegen der Schrittmotoren verzögert folgen.</div></section>
<aside class="side"><section class="panel controls"><div class="eyebrow">Fahrsteuerung</div><div class="phase" id="phase">Lade Status …</div><div class="route"><span>◈</span><span id="route">Warte auf Verbindung</span></div><div class="gear"><div><span>Aktueller Gang</span><div class="muted" id="modeText">Nicht gestartet</div></div><strong id="gear">N</strong></div><div class="buttons"><button id="start" class="btn start" onclick="act('start')">▶ Fahrt starten</button><button id="stop" class="btn stop" onclick="act('stop')">■ Stopp &amp; Nullstellung</button><button id="cal" class="btn cal" onclick="act('referenz')">◎ Nullposition bestätigen</button></div><div id="msg" class="msg" role="status"></div></section>
<section class="panel distance"><div class="eyebrow">Digitaler Kilometerzähler</div><div class="odo"><span id="odometer">000000.0</span> <small>km</small></div><div class="trip"><span>Strecke seit Programmstart</span><b id="trip">0,00 km</b></div><div class="footnote">Kilometerstand wird gespeichert. Der Trip-Zähler beginnt nach einem Programmneustart wieder bei null.</div></section></aside></div></main>
<script>
const $=id=>document.getElementById(id);const clamp=(v,a,b)=>Math.max(a,Math.min(b,v));
let lastOnline=false;
const lengths={};for(const name of ['speedarc','rpmarc']){lengths[name]=$(name).getTotalLength();$(name).style.strokeDasharray='0 '+lengths[name];}
function gauge(name,val,max){let p=clamp(val/max,0,1);let a=lengths[name+'arc'];$(name+'arc').style.strokeDasharray=(a*p)+' '+a;$(name+'needle').style.transform='rotate('+(-132+p*264)+'deg)';}
function display(x){$('speed').textContent=Math.round(x.speed);$('rpm').textContent=Math.round(x.rpm).toLocaleString('de-DE');$('fuel').textContent=Math.round(x.fuel);$('fuellabel').textContent=Math.round(x.fuel)+' %';$('temp').textContent=Math.round(x.temp);$('templabel').textContent=Math.round(x.temp)+' °C';$('fuelbar').style.width=clamp(x.fuel,0,100)+'%';$('tempbar').style.width=clamp(x.temp/120*100,0,100)+'%';gauge('speed',x.speed,240);gauge('rpm',x.rpm,7000);$('phase').textContent=x.phase;$('route').textContent=x.route;$('route2').textContent=x.route;$('gear').textContent=['fahrt','tanken'].includes(x.mode)?x.gear:'N';$('modeText').textContent=x.mode==='fahrt'?'Automatikfahrt':x.mode==='tanken'?'Tankstelle':x.mode==='rueckfahrt'?'Nullstellung':'Standby';$('target').textContent=Math.round(x.target_speed)+' km/h';$('driveTime').textContent=String(Math.floor(x.drive_time/3600)).padStart(2,'0')+':'+String(Math.floor(x.drive_time%3600/60)).padStart(2,'0');$('odometer').textContent=x.odometer_km.toLocaleString('de-DE',{minimumIntegerDigits:6,minimumFractionDigits:1,maximumFractionDigits:1,useGrouping:false});$('trip').textContent=x.trip_km.toLocaleString('de-DE',{minimumFractionDigits:2,maximumFractionDigits:2})+' km';$('modepill').textContent=x.mode==='fahrt'?'● IN FAHRT':x.mode==='tanken'?'⛽ TANKEN':x.mode==='rueckfahrt'?'◌ NULLSTELLUNG':x.mode==='bereit'?'✓ BEREIT':'! REFERENZIEREN';$('modepill').style.color=x.mode==='fahrt'?'#66e3aa':x.mode==='rueckfahrt'?'#f6ba71':'#d2e6fd';$('start').disabled=x.mode!=='bereit';$('stop').disabled=!['fahrt','tanken'].includes(x.mode);$('cal').disabled=x.mode!=='nicht_referenziert';}
async function refresh(){try{let r=await fetch('/api/status',{cache:'no-store'});if(!r.ok)throw Error('Serverstatus '+r.status);let x=await r.json();display(x);$('dot').className='dot online '+(x.mode==='fahrt'?'pulse':'');$('connection').textContent='Pi verbunden · Live';if(!lastOnline)$('msg').textContent='';lastOnline=true;}catch(e){$('dot').className='dot';$('connection').textContent='Verbindung unterbrochen';$('msg').textContent='Keine Verbindung zum Pi';lastOnline=false;}}
async function act(action){if(action==='referenz'&&!confirm('Sind alle VIER Zeiger physisch auf ihrer Nullposition? Erst danach bestätigen.'))return;try{let r=await fetch('/api/'+action,{method:'POST'});let x=await r.json();$('msg').textContent=x.message||x.error||'Unbekannte Antwort';await refresh();}catch(e){$('msg').textContent='Verbindung zum Raspberry Pi fehlgeschlagen';}}
setInterval(refresh,600);refresh();
</script></body></html>
'''

@app.get('/')
def homepage():
    return render_template_string(HTML)

@app.get('/api/status')
def status():
    with lock:
        return jsonify({k: state[k] for k in ('mode','phase','route','speed','rpm','fuel','temp','gear','target_speed','odometer_km','trip_km','drive_time','fuel_stop')})

@app.post('/api/referenz')
def reference():
    with lock:
        if state['mode'] != 'nicht_referenziert':
            return jsonify(error='Kalibrierung nur nach Neustart dieses Programms möglich'), 409
        # Does not move motors; user has physically placed each needle at zero.
        state['mode'] = 'bereit'
        state['phase'] = 'Bereit – Nullposition bestätigt'
    return jsonify(message='Nullposition übernommen')

@app.post('/api/start')
def start():
    with lock:
        if state['mode'] != 'bereit' or not all(m.at_zero() for m in motors):
            return jsonify(error='Noch nicht bereit'), 409
        state['mode'] = 'fahrt'
        state['phase'] = 'Motorstart'
        state['fuel_level'] = 75.0 if state['drive_time'] == 0 else state['fuel_level']
        state['fuel_stop'] = False
        state['segment_until'] = 0
        state['gear'] = 1
        state['shifting_until'] = 0.0
    return jsonify(message='Virtuelle Fahrt gestartet')

@app.post('/api/stop')
def stop():
    with lock:
        if state['mode'] not in ('fahrt','tanken'):
            return jsonify(error='Keine laufende Fahrt'), 409
        state['mode'] = 'rueckfahrt'
        state['phase'] = 'Zeiger fahren zur Nullposition …'
        for k in ('speed','rpm','fuel','temp'):
            state[k] = 0
        apply_targets()
        save_odometer(state['odometer_km'])
    return jsonify(message='Simulation gestoppt. Rückfahrt läuft.')

if __name__ == '__main__':
    t = threading.Thread(target=simulator, daemon=True)
    t.start()
    try:
        print('Öffne im lokalen Netz: http://<PI-IP>:5000')
        app.run(host='0.0.0.0', port=5000, debug=False, threaded=True, use_reloader=False)
    finally:
        stop_threads.set()
        with lock:
            save_odometer(state['odometer_km'])
        for motor in motors:
            motor.close()
