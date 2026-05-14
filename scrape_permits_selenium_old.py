import csv
import time
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager

def scrape_troy_permits_fortified():
    options = webdriver.ChromeOptions()
    driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=options)
    
    all_data = []
    headers = []
    page_num = 1
    
    try:
        url = "https://apps.troymi.gov/PermitsIssued"
        driver.get(url)

        print("\n" + "="*50)
        print("BROWSER PAUSED FOR MANUAL SETUP")
        print("="*50)
        print("1. Apply the 'Right of Way' filter.")
        print("2. Set 'Show 100 entries'.")
        print("3. WAIT at least 5 seconds for the massive table to fully load.")
        input("\nPress ENTER here in the terminal when you are ready to start scraping...")

        table = driver.find_element(By.TAG_NAME, "table")
        th_elements = table.find_elements(By.TAG_NAME, "th")
        headers = [header.get_attribute("textContent").strip() for header in th_elements]
        
        print("\n" + "="*50)
        print("🚀 SCRAPING STARTED!")
        print("🛑 TO STOP EARLY: Press Control+C in this terminal.")
        print("="*50 + "\n")

        while True:
            print(f"Scraping page {page_num}...")
            
            table = driver.find_element(By.TAG_NAME, "table")
            rows = table.find_elements(By.TAG_NAME, "tr")
            
            for row in rows[1:]: 
                cells = row.find_elements(By.TAG_NAME, "td")
                if cells:
                    row_data = [cell.get_attribute("textContent").strip() for cell in cells]
                    all_data.append(row_data)
            
            next_page_num = page_num + 1
            
            # --- FORTIFIED PAGINATION LOGIC ---
            # Using '.' instead of 'text()' ensures it catches the number even if wrapped in a <span>
            xpath = f"//*[(self::a or self::button or self::span or self::li) and normalize-space(.)='{next_page_num}']"
            
            found_next_page = False
            
            # Try to find the button up to 3 times, waiting 2 seconds between tries
            for attempt in range(3):
                try:
                    next_page_element = driver.find_element(By.XPATH, xpath)
                    
                    # Scroll the button into view before clicking (fixes some JS intercept errors)
                    driver.execute_script("arguments[0].scrollIntoView(true);", next_page_element)
                    time.sleep(1) 
                    
                    # Click it
                    driver.execute_script("arguments[0].click();", next_page_element)
                    print(f"Clicked to load page {next_page_num}. Waiting for table to rebuild...")
                    time.sleep(4) # Wait for page to load
                    
                    page_num = next_page_num
                    found_next_page = True
                    break # We found it, break out of the retry loop
                    
                except Exception:
                    print(f"  Attempt {attempt + 1}: Couldn't find page {next_page_num} yet. Waiting 2 seconds...")
                    time.sleep(2)
            
            if not found_next_page:
                print(f"\nCould not find a link for page {next_page_num} after multiple attempts. Assuming we reached the final page.")
                break 

    except KeyboardInterrupt:
        print("\n\n⚠️ MANUAL STOP TRIGGERED. Halting collection immediately...")
        
    except Exception as e:
        print(f"\nAn error occurred: {e}")
        
    finally:
        if headers or all_data:
            filename = "troy_permits_ROW_collected.csv"
            print(f"\nSaving {len(all_data)} records to '{filename}'...")
            with open(filename, 'w', newline='', encoding='utf-8') as f:
                writer = csv.writer(f)
                if headers:
                    writer.writerow(headers)
                writer.writerows(all_data)
            print("✅ Data saved successfully.")
            
        print("Closing browser...")
        driver.quit()

if __name__ == "__main__":
    scrape_troy_permits_fortified()