import sqlite3

def check_and_fix_world():
    conn = sqlite3.connect("gildia.db")
    cursor = conn.cursor()

    # 1. Sprawdzenie struktury tabeli
    print("--- STRUKTURA TABELI 'swiaty' ---")
    cursor.execute("PRAGMA table_info(swiaty)")
    columns = cursor.fetchall()
    for col in columns:
        print(f"Kolumna: {col[1]} (Typ: {col[2]})")
    
    print("\n--- AKTUALNA LISTA ŚWIATÓW W BAZIE ---")
    cursor.execute("SELECT * FROM swiaty")
    rows = cursor.fetchall()
    
    if not rows:
        print("Baza światów jest pusta!")
    else:
        for row in rows:
            print(f"Znaleziono: {row}")

    print("\n--- NAPRAWA / DODAWANIE ---")
    
    # Zaktualizowane zbieranie danych na podstawie Twojego zrzutu ekranu
    print("Wprowadź dane dla nowego wpisu:")
    id_wpisu = input("1. ID (np. 1307435047460016200): ")
    sf_serwer = input("2. Serwer SF (np. s20.sfgame.eu): ")
    kanal_id = input("3. ID Kanału Discord (np. 1518999708553445490): ")
    
    confirm = input(f"\nCzy chcesz dodać/nadpisać wpis '{id_wpisu} | {sf_serwer}'? (t/n): ")
    
    if confirm.lower() == 't':
        try:
            # Wstawiamy 3 wartości zgodnie ze strukturą bazy
            cursor.execute("INSERT OR REPLACE INTO swiaty VALUES (?, ?, ?)", (id_wpisu, sf_serwer, kanal_id))
            conn.commit()
            print(f"✅ Wpis został poprawnie dodany/zaktualizowany.")
        except Exception as e:
            print(f"❌ Błąd podczas zapisu: {e}")
    else:
        print("Operacja anulowana.")

    conn.close()

if __name__ == "__main__":
    check_and_fix_world()
