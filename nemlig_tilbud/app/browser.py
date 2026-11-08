"""Fælles headless Chromium til Nemlig.

Nemlig ligger bag Cloudflare, så alle kald laves fra en rigtig browser
på nemlig.com i stedet for direkte HTTP fra Python.
"""

import logging
import time

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service

LOGGER = logging.getLogger("nemlig.browser")

NEMLIG_URL = "https://www.nemlig.com/"


def create_driver():
    options = Options()
    options.binary_location = "/usr/bin/chromium-browser"

    for argument in [
        "--headless=new",
        "--no-sandbox",
        "--disable-dev-shm-usage",
        "--disable-gpu",
        "--disable-extensions",
        "--window-size=1440,1000",
        "--lang=da-DK",
    ]:
        options.add_argument(argument)

    driver = webdriver.Chrome(service=Service("/usr/bin/chromedriver"), options=options)
    driver.set_page_load_timeout(45)
    driver.set_script_timeout(600)
    return driver


def click_if_present(driver, xpath, label, wait_seconds):
    try:
        element = driver.find_element("xpath", xpath)
        driver.execute_script("arguments[0].click();", element)
        LOGGER.info("%s.", label)
        time.sleep(wait_seconds)
        return True
    except Exception:
        return False


def open_nemlig(driver, url=NEMLIG_URL):
    """Åbn Nemlig, afvis valgfrie cookies og vælg den nye side."""
    driver.get(url)
    time.sleep(3)

    click_if_present(
        driver,
        "//*[self::button or self::a][contains(normalize-space(.), 'Kun nødvendige')]",
        "Valgte kun nødvendige cookies",
        3,
    )
    click_if_present(
        driver,
        "//button[normalize-space(.)='Prøv det nye'] | //a[normalize-space(.)='Prøv det nye']",
        "Valgte det nye Nemlig",
        6,
    )
