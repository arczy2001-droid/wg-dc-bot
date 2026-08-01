import sqlite3

def napraw_baze():
    print("🔧 Łączenie z bazą gildia.db...")
    try:
        conn = sqlite3.connect("gildia.db")
        cursor = conn.cursor()

        # 1. Naprawa tabeli 'swiaty'
        # Zmieniamy stare, błędne nazwy na poprawny adres s20a.sfgame.eu
        stare_nazwy = ('eu20a', 'EU20 AKADEMIA', 'EU20A')
        nowa_nazwa = 's20a.sfgame.eu'

        print(f"🔄 Sprawdzanie i naprawa tabeli 'swiaty'...")
        cursor.execute("UPDATE swiaty SET nazwa = ? WHERE nazwa IN (?, ?, ?)", 
                       (nowa_nazwa, *stare_nazwy))
        
        # 2. Naprawa tabeli 'nieobecnosci' (żeby historia nie przepadła)
        print(f"🔄 Sprawdzanie i naprawa tabeli 'nieobecnosci'...")
        cursor.execute("UPDATE nieobecnosci SET swiat = ? WHERE swiat IN (?, ?, ?)", 
                       (nowa_nazwa, *stare_nazwy))

        # 3. Naprawa tabeli 'raporty'
        print(f"🔄 Sprawdzanie i naprawa tabeli 'raporty'...")
        cursor.execute("UPDATE raporty SET swiat = ? WHERE swiat IN (?, ?, ?)", 
                       (nowa_nazwa, *stare_nazwy))

        # 4. Naprawa tabeli 'czlonkowie'
        print(f"🔄 Sprawdzanie i naprawa tabeli 'czlonkowie'...")
        cursor.execute("UPDATE czlonkowie SET swiat = ? WHERE swiat IN (?, ?, ?)", 
                       (nowa_nazwa, *stare_nazwy))

        conn.commit()
        print("✅ Baza została naprawiona! Wszystkie wpisy 'eu20a' zmienione na 's20a.sfgame.eu'.")
        conn.close()

    except Exception as e:
        print(f"❌ Wystąpił błąd podczas naprawy: {e}")

if __name__ == "__main__":
    napraw_baze()
