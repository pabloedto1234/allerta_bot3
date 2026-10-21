import os
import re
import asyncio
import urllib.request
import locale
from datetime import datetime, timedelta
from telegram.request import HTTPXRequest

from bs4 import BeautifulSoup
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
)

# ==================================================
# CONFIGURAZIONE
# ==================================================

TOKEN = os.environ.get("TOKEN")

URL = (
    "https://mappe.protezionecivile.gov.it/"
    "it/mappe-rischi/bollettino-di-criticita/"
)

INTERVALLO = 60
NOME_JOB = "controllo_allerte_sicilia"

# Chat iscritte al monitoraggio.
chat_ids = set()

# Ultimo risultato inviato a ciascuna chat.
ultimi_inviati = {}

# Evita controlli simultanei del sito.
controllo_in_corso = False


# ==================================================
# FUNZIONE PER OTTENERE LA DATA DI DOMANI IN ITALIANO
# ==================================================

def ottieni_data_domani():
    """Restituisce la data di domani formattata (es. 'giovedì 22 ottobre')
       forzando la lingua italiana anche se il server è estero.
    """
    # Tenta di impostare la lingua italiana sul sistema
    for loc in ['it_IT.UTF-8', 'it_IT', 'Italian_Italy']:
        try:
            locale.setlocale(locale.LC_TIME, loc)
            break
        except locale.Error:
            continue

    # Calcola la data di domani
    domani = datetime.now() + timedelta(days=1)
    
    # Esempio di formato comune nei bollettini (es. "22 ottobre 2026" o simili)
    # Restituiamo il giorno e il mese. Adattalo se sul sito la data è scritta diversamente.
    return domani


# ==================================================
# LETTURA E ANALISI DEL BOLLETTINO
# ==================================================

def find_allerta2():
    """Scarica il bollettino ed estrae le allerte della Sicilia per la giornata di domani."""

    req = urllib.request.Request(
        URL,
        headers={"User-Agent": "Mozilla/5.0"}
    )

    with urllib.request.urlopen(req, timeout=60) as response:
        html = response.read()

    soup = BeautifulSoup(html, "html.parser")

    pannello = soup.find(
        "div",
        {
            "role": "tabpanel",
            "id": "Tabset1Panel1"
        }
    )

    if pannello is None:
        raise ValueError(
            "Pannello del bollettino non trovato. "
            "La struttura del sito potrebbe essere cambiata."
        )

    testo = pannello.get_text(" ", strip=True)
    testo = re.sub(r"\s+", " ", testo)

    if not re.search(r"\bALLERTA\b", testo, re.IGNORECASE):
        raise ValueError(
            "Il testo scaricato non contiene allerte."
        )

    # --- VERIFICA DELLA DATA DI DOMANI NEL TESTO ---
    domani = ottieni_data_domani()
    giorno_num = domani.strftime("%d").lstrip("0") # es. "22" anziché "02" se < 10
    
    # Ricaviamo il nome del mese in italiano
    mesi_it = {
        1: "gennaio", 2: "febbraio", 3: "marzo", 4: "aprile",
        5: "maggio", 6: "giugno", 7: "luglio", 8: "agosto",
        9: "settembre", 10: "ottobre", 11: "novembre", 12: "dicembre"
    }
    mese_nome = mesi_it[domani.month]
    
    # Pattern flessibile per cercare la data di domani nel testo (es. "22 ottobre" o "22/10")
    pattern_data = rf"\b{giorno_num}\s+{mese_nome}\b"
    
    if not re.search(pattern_data, testo, re.IGNORECASE):
        print(f"Attenzione: La data di domani ({giorno_num} {mese_nome}) non è stata ancora pubblicata o trovata nel bollettino.")
        # Se preferisci bloccare l'invio finché non c'è la data giusta, puoi ritornare una lista vuota o sollevare eccezione:
        return []

    pattern_blocchi = (
        r"((?:ORDINARIA|MODERATA|ELEVATA)"
        r"\s+CRITICIT[AÀ]\s*'?.*?)"
        r"(?=(?:ORDINARIA|MODERATA|ELEVATA)"
        r"\s+CRITICIT[AÀ]|\Z)"
    )

    blocchi = re.findall(
        pattern_blocchi,
        testo,
        re.IGNORECASE
    )

    allerte = []

    for blocco in blocchi:

        # Considera solo i blocchi che menzionano la Sicilia.
        if not re.search(
            r"\bSicilia\s*:",
            blocco,
            re.IGNORECASE
        ):
            continue

        if not re.search(
            r"\bNord[-–‑]Orientale\s*,\s*versante\s+ionico\b",
            blocco,
            re.IGNORECASE
        ):
            continue

        rischio_match = re.search(
            r"PER RISCHIO\s+(.+?)\s*/\s*ALLERTA",
            blocco,
            re.IGNORECASE
        )

        livello_match = re.search(
            r"/\s*ALLERTA\s*:?\s*"
            r"(GIALLA|GIALLO|ARANCIONE|ROSSA|ROSSO)\b",
            blocco,
            re.IGNORECASE
        )

        if not rischio_match or not livello_match:
            continue

        rischio = rischio_match.group(1).strip().upper()
        livello = livello_match.group(1).upper()

        # Uniforma i nomi dei livelli.
        if livello == "GIALLA":
            livello = "GIALLO"
        elif livello == "ROSSA":
            livello = "ROSSO"

        risultato = {
            "rischio": rischio,
            "livello": livello
        }

        # Evita eventuali duplicati.
        if risultato not in allerte:
            allerte.append(risultato)

    if not blocchi:
        raise ValueError(
            "Nessun blocco di criticità riconosciuto. "
            "Controlla la struttura del bollettino."
        )

    return allerte


# ==================================================
# CONTROLLO PERIODICO E INVIO TELEGRAM
# ==================================================

async def find_allerta(
    context: ContextTypes.DEFAULT_TYPE
):
    """Controlla il bollettino e invia notifiche solo se necessario."""

    global controllo_in_corso

    if controllo_in_corso:
        print("Controllo già in corso: salto questa esecuzione.")
        return

    if not chat_ids:
        print("Nessuna chat iscritta al monitoraggio.")
        return

    controllo_in_corso = True

    try:
        allerte = await asyncio.to_thread(find_allerta2)

        allerte.sort(
            key=lambda a: (a["rischio"], a["livello"])
        )

        firma = tuple(
            (a["rischio"], a["livello"])
            for a in allerte
        )

        if allerte:
            righe = [
                "🚨 BOLLETTINO ALLERTE - SICILIA PER LA GIORNATA DI DOMANI SANTA TERESA DI RIVA",
                "",
            ]

            for allerta in allerte:
                righe.append(
                    f"• Rischio {allerta['rischio']}: "
                    f"ALLERTA {allerta['livello']}"
                )

            messaggio = "\n".join(righe)
        else:
            messaggio = None

        for chat_id in list(chat_ids):

            if ultimi_inviati.get(chat_id) == firma:
                continue

            try:
                if messaggio is not None:
                    await context.bot.send_message(
                        chat_id=chat_id,
                        text=messaggio
                    )

                    ultimi_inviati[chat_id] = firma

                    print(
                        f"Notifica inviata alla chat {chat_id}."
                    )

            except Exception as e:
                print(
                    f"Errore invio alla chat {chat_id}: {e}"
                )

    except Exception as e:
        print(f"Errore durante il controllo: {e}")

    finally:
        controllo_in_corso = False


async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    chat_id = update.effective_chat.id

    nuova_iscrizione = chat_id not in chat_ids
    chat_ids.add(chat_id)

    if context.job_queue.get_jobs_by_name(NOME_JOB) == ():
        context.job_queue.run_repeating(
            find_allerta,
            interval=INTERVALLO,
            first=INTERVALLO,
            name=NOME_JOB
        )

    await update.effective_message.reply_text(
        "✅ Monitoraggio delle allerte attivato.\n\n"
        "Controllerò il bollettino subito e poi ogni ora. "
        "Riceverai una notifica quando cambia il risultato rilevato."
    )

    if nuova_iscrizione:
        await find_allerta(context)


async def stop(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    chat_id = update.effective_chat.id

    chat_ids.discard(chat_id)
    ultimi_inviati.pop(chat_id, None)

    await update.effective_message.reply_text(
        "🛑 Monitoraggio delle allerte disattivato."
    )

    if not chat_ids:
        for job in context.job_queue.get_jobs_by_name(NOME_JOB):
            job.schedule_removal()


def main():
    if not TOKEN:
        raise RuntimeError(
            "Token mancante. Imposta la variabile "
            "d'ambiente TOKEN prima dell'avvio."
        )

    proxy = "http://proxy.server:3128"

    request = HTTPXRequest(proxy=proxy)
    updates_request = HTTPXRequest(proxy=proxy)

    application = (
        ApplicationBuilder()
        .token(TOKEN)
        .request(request)
        .get_updates_request(updates_request)
        .build()
    )
    if application.job_queue is None:
        raise RuntimeError(
            'JobQueue non disponibile. Installa la libreria con: '
            'python -m pip install "python-telegram-bot[job-queue]"'
        )

    application.add_handler(
        CommandHandler("start", start)
    )

    application.add_handler(
        CommandHandler("stop", stop)
    )

    print("Bot avviato. Premi Ctrl+C per fermarlo.")

    application.run_polling()


if __name__ == "__main__":
    main()
