import time
from gpiozero import OutputDevice
from time import sleep

time.sleep(2)
# GPIO-Belegung (BCM)
MOTOREN = {
    1: [17, 18, 27, 22],
    2: [23, 24, 25, 5],
    3: [6, 12, 13, 19],
    4: [16, 20, 21, 26]
}

# Dauer pro LED in Sekunden
LED_DAUER = 0.5

motoren = {}

try:
    # GPIO-Ausgänge vorbereiten
    for nummer, pins in MOTOREN.items():
        motoren[nummer] = [
            OutputDevice(pin, initial_value=False)
            for pin in pins
        ]

    print("LED-Test startet!")

    for nummer, pins in motoren.items():
        print(f"\nTeste Treiber {nummer}")

        for index, pin in enumerate(pins, start=1):
            print(f"  IN{index} EIN")
            pin.on()
            sleep(LED_DAUER)

            pin.off()
            sleep(0.2)

        print(f"Treiber {nummer} fertig")
        sleep(1)

    print("\nAlle 4 Treiber getestet!")

finally:
    # Alle Ausgänge ausschalten
    for pins in motoren.values():
        for pin in pins:
            pin.close()