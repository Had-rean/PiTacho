#!/usr/bin/env python3
"""Passat 35i desktop simulator (Pi 5, 4x 28BYJ-48 + ULN2003)."""
import math
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

app = Flask(__name__)
lock = threading.RLock()
stop_threads = threading.Event()
state = dict(mode='nicht_referenziert', phase='Bitte Nullposition bestätigen',
             speed=0.0, rpm=0.0, fuel=0.0, temp=0.0,
             route='Landstraße', ready=False, segment_until=0.0, target_speed=0.0,
             fuel_stop=False, fuel_level=75.0, drive_time=0.0,
             gear=1, shifting_until=0.0)

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
    while not stop_threads.wait(0.1):
        now = time.monotonic()
        dt = min(now-last, 0.5)
        last = now
        with lock:
            mode = state['mode']
            if mode == 'fahrt':
                state['drive_time'] += dt
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


HTML = '''<!doctype html><html lang="de"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>Passat 35i Simulator</title>
<style>body{font:16px system-ui;background:#101720;color:#e8eef7;margin:0;padding:30px}main{max-width:780px;margin:auto}h1{margin-bottom:6px}small,p{color:#aab6c6}.panel{background:#1b2735;padding:22px;border-radius:16px;margin:18px 0}.grid{display:grid;grid-template-columns:repeat(2,1fr);gap:12px}.tile{background:#263749;border-radius:12px;padding:17px}.tile strong{font-size:30px;display:block;margin-top:8px}button{font:inherit;padding:14px 22px;border:0;border-radius:10px;margin-right:8px;margin-bottom:8px;font-weight:700;cursor:pointer}#start{background:#63dfaa;color:#10251e}#stop{background:#f6ac91;color:#30160e}#cal{background:#92b4ef;color:#07182e}button:disabled{opacity:.35;cursor:default}#msg{min-height:22px;color:#f5c38a}@media(max-width:460px){.grid{grid-template-columns:1fr}}</style></head>
<body><main><h1>Passat 35i</h1><p>Schreibtisch-Fahrsimulator · Raspberry Pi 5</p><section class="panel"><h2 id="phase">Lade Status …</h2><p id="route"></p><p id="gear"></p><button id="cal" onclick="act('referenz')">Nullposition bestätigen</button><button id="start" onclick="act('start')">Fahrt starten</button><button id="stop" onclick="act('stop')">Stopp &amp; Nullstellung</button><div id="msg"></div></section><section class="grid"><div class="tile">Geschwindigkeit<strong id="speed">0 km/h</strong></div><div class="tile">Drehzahl<strong id="rpm">0 U/min</strong></div><div class="tile">Tank<strong id="fuel">0 %</strong></div><div class="tile">Temperatur<strong id="temp">0 °C</strong></div></section><p>Die Werte sind simulierte Sollwerte. Wegen der trägen Motoren können die tatsächlichen Zeiger nachlaufen.</p></main>
<script>
async function refresh(){try{let r=await fetch('/api/status');let x=await r.json();document.getElementById('phase').textContent=x.phase;document.getElementById('route').textContent='Modus: '+x.mode+' · Strecke: '+x.route;document.getElementById('gear').textContent='Gang: '+x.gear+' · Zielgeschwindigkeit: '+Math.round(x.target_speed)+' km/h';for(let k of ['speed','rpm','fuel','temp'])document.getElementById(k).textContent=Math.round(x[k])+' '+({speed:'km/h',rpm:'U/min',fuel:'%',temp:'°C'}[k]);document.getElementById('cal').disabled=x.mode!=='nicht_referenziert';document.getElementById('start').disabled=x.mode!=='bereit';document.getElementById('stop').disabled=!['fahrt','tanken'].includes(x.mode);}catch(e){document.getElementById('msg').textContent='Keine Verbindung zum Pi';}}
async function act(action){if(action==='referenz'&&!confirm('Sind ALLE Zeiger jetzt physisch exakt auf Null? Nur dann bestätigen!'))return;try{let r=await fetch('/api/'+action,{method:'POST'});let x=await r.json();document.getElementById('msg').textContent=x.message||x.error||'';refresh();}catch(e){document.getElementById('msg').textContent='Verbindungsfehler';}}
setInterval(refresh,600);refresh();
</script></body></html>'''

@app.get('/')
def homepage():
    return render_template_string(HTML)

@app.get('/api/status')
def status():
    with lock:
        return jsonify({k: state[k] for k in ('mode','phase','route','speed','rpm','fuel','temp','gear','target_speed')})

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
    return jsonify(message='Simulation gestoppt. Rückfahrt läuft.')

if __name__ == '__main__':
    t = threading.Thread(target=simulator, daemon=True)
    t.start()
    try:
        print('Öffne im lokalen Netz: http://<PI-IP>:5000')
        app.run(host='0.0.0.0', port=5000, debug=False, threaded=True, use_reloader=False)
    finally:
        stop_threads.set()
        for motor in motors:
            motor.close()
