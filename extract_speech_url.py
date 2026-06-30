from seleniumbase import BaseCase
from datetime import datetime
from dateutil import parser
import time
from seleniumbase import SB
import json
from seleniumbase import BaseCase
from datetime import datetime
from dateutil import parser
import time
from seleniumbase import SB
import json


class PMSpeechScroller(BaseCase):

    def scroll_and_collect(self, end_date_str):
        end_date = datetime.strptime(end_date_str, "%Y-%m-%d").date()
        collected = {}
        last_count = 0
        same_count_retries = 0
        max_same_count_retries = 8

        self.open("https://www.pmindia.gov.in/en/tag/pmspeech/")
        self.wait_for_element("ul.news-holder", timeout=30)

        while True:
            items = self.find_elements('ul.news-holder li[class="6u"]')

            for item in items:
                try:
                    link = item.find_element(
                        "css selector",
                        ".news-description h3 a"
                    ).get_attribute("href")

                    date_text = item.find_element(
                        "css selector",
                        ".news-description span.date"
                    ).text.strip()

                    parsed_date = parser.parse(date_text).date()

                    if link not in collected:
                        collected[link] = parsed_date

                except Exception:
                    continue

            dates = list(collected.values())
            if dates and min(dates) <= end_date:
                break

            current_count = len(collected)

            if current_count == last_count:
                same_count_retries += 1
            else:
                same_count_retries = 0

            if same_count_retries >= max_same_count_retries:
                break

            last_count = current_count

            self.execute_script(
                "window.scrollBy(0, window.innerHeight * 0.8);"
            )
            time.sleep(3)

            if current_count % 20 == 0:
                time.sleep(2)

        return collected


with SB(uc=True, headless=True) as sb:
    scroller = PMSpeechScroller()
    scroller.driver = sb.driver
    scroller.open = sb.open
    scroller.find_elements = sb.find_elements
    scroller.wait_for_element = sb.wait_for_element
    scroller.execute_script = sb.execute_script
    
    data = scroller.scroll_and_collect(end_date_str="2014-01-01")

    with open("speech_urls.json", "w", encoding="utf-8") as f:
        json.dump(
            {url: date.isoformat() for url, date in data.items()},
            f,
            ensure_ascii=False,
            indent=2
        )

    print(f"Saved {len(data)} speech URLs to speech_urls.json")