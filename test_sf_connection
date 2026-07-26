import asyncio
import logging
import os
from dotenv import load_dotenv

# Wczytanie zmiennych środowiskowych z pliku .env (opcjonalnie)
load_dotenv()

# Konfiguracja czytelnych logów w konsoli
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S"
)
logger = logging.getLogger("SF_Test")

async def main():
    """
    Skrypt testowy do weryfikacji logowania oraz odczytu nazwy gildii i statusu walk.
    Używa biblioteki sf-api (https://github.com/the-marenga/sf-api)
    """
    
    # -------------------------------------------------------------------
    # DANE LOGOWANIA (Podmień na własne dane testowe lub użyj pliku .env)
    # -------------------------------------------------------------------
    USERNAME = os.getenv("SF_USERNAME", "ArczY")
    PASSWORD = os.getenv("SF_PASSWORD", "Artur2001")
    SERVER = os.getenv("SF_SERVER", "s20.sfgame.eu") # np. w1.sfgame.net, s1.sfgame.pl itp.
    
    logger.info("=== START TESTU POŁĄCZENIA S&F API ===")
    logger.info(f"Próba logowania na serwer: {SERVER} jako użytkownik: {USERNAME}")

    try:
        # Importujemy klienta z biblioteki sf-api
        # UWAGA: Przed uruchomieniem zainstaluj bibliotekę: pip install git+https://github.com/the-marenga/sf-api.git
        from sf_api import Client
        
        # 1. Inicjalizacja klienta gry dla konkretnego świata
        async with Client(server=SERVER) as client:
            
            # 2. Logowanie do gry
            login_success = await client.login(username=USERNAME, password=PASSWORD)
            
            if not login_success:
                logger.error("❌ Błąd logowania! Sprawdź login, hasło oraz adres serwera.")
                return

            logger.info("✅ Pomyślnie zalogowano do serwera S&F!")

            # 3. Pobranie danych gracza i jego gildii
            # Odczytujemy stan zalogowanej postaci
            player_data = await client.get_player()
            guild_data = getattr(player_data, "guild", None)

            if not guild_data:
                logger.warning("⚠️ Postać zalogowała się poprawnie, ale nie należy do żadnej gildii!")
                return

            # 4. Wyświetlenie wyników w konsoli
            guild_name = getattr(guild_data, "name", "Brak nazwy")
            guild_members_count = len(getattr(guild_data, "members", []))
            
            # Pobieramy informacje o ewentualnych atakach/obronach
            has_attack = getattr(guild_data, "has_attack", False)
            has_defense = getattr(guild_data, "has_defense", False)

            logger.info("--------------------------------------------------")
            logger.info(f"🏰 Nazwa Gildii:   [{guild_name}]")
            logger.info(f"👥 Liczba członków: {guild_members_count}")
            logger.info(f"⚔️ Wyznaczony atak:  {'TAK' if has_attack else 'NIE'}")
            logger.info(f"🛡️ Zaplanowana obrona: {'TAK' if has_defense else 'NIE'}")
            logger.info("--------------------------------------------------")
            logger.info("🎉 Test połączenia zakończony sukcesem! API działa prawidłowo.")

    except ImportError:
        logger.error(
            "❌ Brak zainstalowanej biblioteki sf-api!\n"
            "Zainstaluj ją komendą:\n"
            "pip install git+https://github.com/the-marenga/sf-api.git"
        )
    except Exception as e:
        logger.error(f"❌ Wystąpił nieoczekiwany błąd podczas połączenia: {e}")

if __name__ == "__main__":
    # Uruchomienie pętli asynchronicznej
    asyncio.run(main())
