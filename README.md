# 🛠️ Majster-AI: Automotive UDS Diagnostic Agent via MCP

![Python](https://img.shields.io/badge/Python-3.10%2B-blue)
![Protocol](https://img.shields.io/badge/Protocol-UDS%20%7C%20CAN-orange)
![AI](https://img.shields.io/badge/AI-LLM%20%7C%20LangGraph-purple)
![MCP](https://img.shields.io/badge/Architecture-MCP%20%28Model%20Context%20Protocol%29-brightgreen)

**Majster-AI** to zaawansowany agent sztucznej inteligencji służący do interaktywnej diagnostyki samochodowej, analizy kodów usterek (DTC) oraz wsparcia serwisowego. Projekt łączy fizyczny interfejs pojazdu (J2534/Tactrix) z modelami językowymi (LLM) przy użyciu Model Context Protocol (MCP), tworząc pomost między cyfrowym mózgiem AI a fizycznymi modułami samochodu.

---

## 🎯 Główne funkcje

* **Diagnostyka głęboka (UDS po CAN):** Pełny dostęp `READ_ONLY` do wszystkich modułów w aucie (ECM, ABS, Haldex, BCM, Terrain Response) z pominięciem ograniczeń standardowego OBD2.
* **Architektura MCP (Model Context Protocol):** System oparty na oddzielnych serwerach narzędziowych, co pozwala LLM-owi na dynamiczne odpytywanie samochodu w czasie rzeczywistym.
* **RAG (Retrieval-Augmented Generation):** Wbudowana baza wektorowa zawierająca instrukcje serwisowe (Workshop Manuals). Agent automatycznie dopasowuje kody DTC do instrukcji demontażu i naprawy.
* **Web Search Integration:** Możliwość przeszukiwania specjalistycznych forów (np. freel2.com) w locie, aby diagnozować nietypowe usterki.
* **🛡️ Bezpieczeństwo (Human-in-the-Loop):** Architektura gwarantująca bezpieczeństwo pojazdu. Agent działa domyślnie w trybie `READ_ONLY`. Jakiekolwiek operacje zapisu (np. kasowanie błędów, adaptacje przepustnicy) wymagają jawnego autoryzowania przez operatora (HITL).

## 🏗️ Architektura Systemu

System opiera się na orkiestracji (np. LangGraph) zarządzającej trzema niezależnymi serwerami MCP:

1. **`Car_Interface_MCP`**: Serwer komunikacyjny napisany w Pythonie (`udsoncan`, `python-can`). Tłumaczy zapytania JSON od LLM na fizyczne ramki CAN wysyłane do interfejsu (np. Tactrix Openport 2.0).
2. **`RAG_Workshop_MCP`**: Silnik wektorowy przeszukujący dokumentację techniczną pojazdu na podstawie zidentyfikowanych problemów.
3. **`Web_Search_MCP`**: Serwer integrujący zewnętrzne API wyszukiwarki do pozyskiwania wiedzy z sieci.

## 💻 Wymagania sprzętowe i programowe

* **Hardware:** 
  * Interfejs diagnostyczny J2534 PassThru (np. Tactrix Openport 2.0, Autocom CDP+ z odpowiednimi sterownikami).
  * Samochód obsługujący standard CAN / UDS (testowane na architekturze JLR / Ford).
* **Software:**
  * Komputer, Raspberry Pi lub środowisko hybrydowe (np. Linux/XFCE na ARM64) zdolne do obsługi portów szeregowych / USB.
  * Python 3.10+

## 🚀 Szybki start (Instalacja)

1. Sklonuj repozytorium:
   ```bash
   git clone [https://github.com/twoja-nazwa/majster-ai.git](https://github.com/twoja-nazwa/majster-ai.git)
   cd majster-ai
