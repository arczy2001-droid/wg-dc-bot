import sqlite3

def check_and_fix_world():
    conn = sqlite3.connect("gildia.db")
    cursor = conn.cursor()

    print("--- AKTUALNA LISTA ŚWIATÓW W BAZIE ---")
    cursor.execute("SELECT nazwa, kanal_id FROM swiaty")
    rows = cursor.fetchall()
    
    if not rows:
        print("Baza światów jest pusta!")
    else:
        for row in rows:
            print(f"Znaleziono: {row[0]} -> Kanał ID: {row[1]}")

    print("\n--- NAPRAWA ---")
    
    # Dane dla brakującego świata (podstaw tutaj właściwe ID kanału)
    nazwa_swiata = "eu20a"
    kanal_id = "123456789012345678"  # <--- PODMIEŃ NA ID KANAŁU DLA AKADEMII
    
    confirm = input(f"Czy chcesz dodać/nadpisać świat '{nazwa_swiata}' z kanałem {kanal_id}? (t/n): ")
    
    if confirm.lower() == 't':
        try:
            cursor.execute("INSERT OR REPLACE INTO swiaty VALUES (?, ?)", (nazwa_swiata, kanal_id))
            conn.commit()
            print(f"✅ Świat {nazwa_swiata} został poprawnie dodany/zaktualizowany.")
        except Exception as e:
            print(f"❌ Błąd podczas zapisu: {e}")
    else:
        print("Operacja anulowana.")

    conn.close()

if __name__ == "__main__":
    check_and_fix_world()