import asyncio
import logging
import aiohttp
import os

# Konfiguracja czytelnych logów w konsoli
logging.basicConfig(
    level=logging.INFO, 
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S"
)
logger = logging.getLogger("SF_Test")

async def main():
    # ---------------------------------------------------------
    # KONFIGURACJA TESTU
    # ---------------------------------------------------------
    # 1. Podaj swój aktualny serwer gry (np. w1.sfgame.net, w54.sfgame.net)
    SERVER = "w1.sfgame.net" 
    
    # 2. Skopiuj swoje "żądanie" z przeglądarki (tzw. Payload)
    # INSTRUKCJA:
    # a) Zaloguj się do gry w przeglądarce i wciśnij F12.
    # b) Przejdź do zakładki "Sieć" (Network) i kliknij przycisk "Gildia" w grze.
    # c) Na liście w F12 pojawi się plik "req.php". Kliknij w niego.
    # d) Wejdź w zakładkę "Payload" (Żądanie) -> wybierz "View source" (pokaż źródło).
    # e) Skopiuj cały ciąg zaczynający się od "req=..." i wklej go poniżej między cudzysłowy.
    
    PAYLOAD_Z_GRA = "" 

    url = f"https://{SERVER}/cmd.php"
    
    logger.info("=== START TESTU POŁĄCZENIA Z S&F ===")
    logger.info(f"🌍 Serwer docelowy: {url}")
    
    try:
        # Inicjalizacja sesji HTTP
        async with aiohttp.ClientSession() as session:
            
            if not PAYLOAD_Z_GRA:
                # Jeśli nie wklejono danych z F12, wykonujemy tylko próbny "ping"
                logger.warning("⚠️ Zmienna PAYLOAD_Z_GRA jest pusta!")
                logger.info("Wykonuję testowy 'ping' do serwera, aby sprawdzić łączność...")
                
                async with session.get(url) as response:
                    if response.status == 200:
                        logger.info("✅ Serwer odpowiedział poprawnie (HTTP 200).")
                        logger.info("👉 Aby zobaczyć dane gildii, uzupełnij PAYLOAD_Z_GRA w kodzie i odpal ponownie.")
                    else:
                        logger.error(f"❌ Serwer odrzuca połączenie. Status HTTP: {response.status}")
            
            else:
                # Wysyłamy żądanie udając standardową przeglądarkę internetową
                headers = {
                    'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36'
                }
                
                logger.info("📤 Wysyłanie zaszyfrowanego żądania o stan gildii...")
                
                async with session.post(url, data=PAYLOAD_Z_GRA, headers=headers) as response:
                    if response.status == 200:
                        data = await response.text()
                        logger.info("✅ Otrzymano odpowiedź od serwera!")
                        logger.info("-" * 60)
                        
                        # Zwracany tekst z S&F to zazwyczaj długi ciąg oddzielony znakami / lub ;
                        # Wyświetlamy pierwsze 500 znaków, aby zweryfikować czy widać w nim informacje
                        fragment = data[:500] if len(data) > 500 else data
                        logger.info(f"📄 ODCZYTANE DANE:\n{fragment}")
                        logger.info("-" * 60)
                        
                        if "Error" in data or data.startswith("false"):
                            logger.warning("⚠️ Serwer zwrócił błąd. Sesja mogła wygasnąć - skopiuj nowy payload z F12.")
                        else:
                            logger.info("🎉 Sukces! Pomyślnie zescrapowano dane z serwera metodą read-only.")
                    else:
                        logger.error(f"❌ Błąd HTTP {response.status} podczas pobierania danych.")
                        
    except Exception as e:
        logger.error(f"❌ Wystąpił błąd krytyczny połączenia: {e}")

if __name__ == "__main__":
    asyncio.run(main())