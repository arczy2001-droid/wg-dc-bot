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

    print("\n--- NAPRAWA ---")
    
    # Podstaw tutaj dane
    nazwa_swiata = "eu20a"
    kanal_id = "123456789012345678"  # <--- ID KANAŁU
    # Jeśli 3. kolumna to np. 'sf_server', dopisz jej wartość poniżej:
    trzecia_wartosc = None 
    
    confirm = input(f"Czy chcesz dodać/nadpisać świat '{nazwa_swiata}'? (t/n): ")
    
    if confirm.lower() == 't':
        try:
            # Wstawiamy 3 wartości. Jeśli 3. kolumna to tekst, możesz wpisać tam np. "s20" zamiast None
            cursor.execute("INSERT OR REPLACE INTO swiaty VALUES (?, ?, ?)", (nazwa_swiata, kanal_id, trzecia_wartosc))
            conn.commit()
            print(f"✅ Świat {nazwa_swiata} został poprawnie dodany/zaktualizowany.")
        except Exception as e:
            print(f"❌ Błąd podczas zapisu: {e}")
    else:
        print("Operacja anulowana.")

    conn.close()

if __name__ == "__main__":
    check_and_fix_world()
