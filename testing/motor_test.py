from gpiozero import OutputDevice
from time import sleep

# GPIO-Belegung (BCM)
MOTOREN = {
    1: [17, 18, 27, 22],
    2: [23, 24, 25, 5],
    3: [6, 12, 13, 19],
    4: [16, 20, 21, 26]
}

# Halbschritt-Sequenz
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

# Einstellungen
SCHRITTE_360 = 4076
WINKEL = 10
SCHRITTE = round(SCHRITTE_360 * WINKEL / 360)
GESCHWINDIGKEIT = 0.0025

motoren = {}

try:
    for nummer, pins in MOTOREN.items():
        motoren[nummer] = [
            OutputDevice(pin, initial_value=False)
            for pin in pins
        ]

    def drehen(nummer, schritte):
        pins = motoren[nummer]

        print(f"Motor {nummer} dreht ca. {WINKEL} Grad")

        for schritt in range(abs(schritte)):
            index = (
                schritt % 8
                if schritte >= 0
                else (-schritt) % 8
            )

            muster = SEQUENZ[index]

            for pin, wert in zip(pins, muster):
                if wert:
                    pin.on()
                else:
                    pin.off()

            sleep(GESCHWINDIGKEIT)

        # Motor abschalten
        for pin in pins:
            pin.off()

        print(f"Motor {nummer} fertig")

    # Alle vier Motoren nacheinander testen
    for nummer in range(1, 5):
        drehen(nummer, SCHRITTE)
        sleep(1)

    print("Alle Motoren haben sich ca. 10 Grad gedreht!")

finally:
    for pins in motoren.values():
        for pin in pins:
            pin.close()