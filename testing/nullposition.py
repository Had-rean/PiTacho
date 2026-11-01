from gpiozero import OutputDevice
from time import sleep
import threading

# Motoren / GPIO-Pins (BCM)
MOTOREN = {
    1: {
        "name": "Drehzahlmesser",
        "pins": [17, 18, 27, 22],
        "richtung": -1
    },
    2: {
        "name": "Geschwindigkeit",
        "pins": [23, 24, 25, 5],
        "richtung": -1
    },
    3: {
        "name": "Tankanzeige",
        "pins": [6, 12, 13, 19],
        "richtung": 1
    },
    4: {
        "name": "Temperatur",
        "pins": [16, 20, 21, 26],
        "richtung": -1
    }
}

# Halbschritt-Sequenz
SEQUENZ = [
    [1, 0, 0, 0],
    [1, 1, 0, 0],
    [0, 1, 0, 0],
    [0, 1, 1, 0],
    [0, 0, 1, 0],
    [0, 0, 0, 1],
    [0, 0, 0, 1],
    [1, 0, 0, 1]
]

# Korrigierte Halbschritt-Sequenz
SEQUENZ = [
    [1, 0, 0, 0],
    [1, 1, 0, 0],
    [0, 1, 0, 0],
    [0, 1, 1, 0],
    [0, 0, 1, 0],
    [0, 0, 1, 1],
    [0, 0, 0, 1],
    [1, 0, 0, 1]
]

# Höherer Wert = langsamer
GESCHWINDIGKEIT = 0.004

gpio_motoren = {}
stop_event = threading.Event()


def motor_stoppen(pins):
    for pin in pins:
        pin.off()


def motor_drehen(nummer):
    motor = MOTOREN[nummer]
    pins = gpio_motoren[nummer]
    richtung = motor["richtung"]

    position = 0

    print(f"\n{motor['name']} bewegt sich.")
    print("ENTER drücken, um zu stoppen!")

    while not stop_event.is_set():
        position = (position + richtung) % 8
        muster = SEQUENZ[position]

        for pin, wert in zip(pins, muster):
            if wert:
                pin.on()
            else:
                pin.off()

        sleep(GESCHWINDIGKEIT)

    motor_stoppen(pins)
    print(f"{motor['name']} gestoppt.")


try:
    for nummer, motor in MOTOREN.items():
        gpio_motoren[nummer] = [
            OutputDevice(pin, initial_value=False)
            for pin in motor["pins"]
        ]

    print("=== PASSAT 35i TACHO NULLPOSITION ===")

    while True:
        print("\nMotor auswählen:")
        print("1 = Drehzahlmesser")
        print("2 = Geschwindigkeit")
        print("3 = Tankanzeige")
        print("4 = Temperatur")
        print("q = Beenden")

        auswahl = input("\nAuswahl: ").strip().lower()

        if auswahl == "q":
            break

        if auswahl not in ["1", "2", "3", "4"]:
            print("Ungültige Eingabe.")
            continue

        nummer = int(auswahl)
        stop_event.clear()

        thread = threading.Thread(
            target=motor_drehen,
            args=(nummer,)
        )

        thread.start()

        try:
            input()
        finally:
            stop_event.set()
            thread.join()

finally:
    stop_event.set()

    for pins in gpio_motoren.values():
        motor_stoppen(pins)
        for pin in pins:
            pin.close()

    print("\nProgramm beendet.")