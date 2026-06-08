import requests
import re
from bs4 import BeautifulSoup

def _extract_download_info(div, link_selector, filename_selector):
    download_link = div.select_one(link_selector)
    filename_div = div.select_one(filename_selector)
    
    if not download_link or not filename_div:
        return None

    href = download_link.get("data-href") or download_link.get("href")
    filename = filename_div.get_text(strip=True)

    if not href or not filename:
        return None

    match = re.search(r'/downloadables/(\d+)', href)
    if not match:
        return None

    return [match.group(1), filename]

def _crawling_base(url, cookie, selectors, shortlist, thumblist, product_only_filter=None):
    response = requests.get(url=url, cookies=cookie, timeout=30)
    response.raise_for_status()
    html = response.content

    download_url_list = []
    product_info_list = []
    soup = BeautifulSoup(html, "html.parser")
    
    product_divs = soup.find_all(selectors['product_div_tag'], class_=selectors['product_div_class'])
    
    for product_div in product_divs:
        if 'product_info_index' in selectors:
            product_info_elements = product_div.select(selectors['product_info_selector'])
            if len(product_info_elements) <= selectors['product_info_index']:
                continue
            product_info = product_info_elements[selectors['product_info_index']]
        else:
            product_info = product_div.select_one(selectors['product_info_selector'])

        if not product_info:
            continue
            
        product_name = product_info.get_text()
        product_url = product_info.get("href")
        
        if product_only_filter:
            match = re.search(r'/items/(\d+)', product_url)
            if not match or match.group(1) not in product_only_filter:
                continue
        
        product_info_list.append([product_name, product_url])

        thumb_link = product_div.select_one(selectors['thumb_selector'])
        if thumb_link and thumblist is not None:
            thumblist.append(thumb_link.get("src"))
        
        download_item_divs = product_div.select(selectors['download_item_selector'])
        for div in download_item_divs:
            info = _extract_download_info(div, selectors['download_link_selector'], selectors['filename_selector'])
            if info:
                download_url_list.append(info)
                if shortlist is not None:
                    shortlist.append(info[0])
            
    return download_url_list, product_info_list

def crawling(order_num, product_only, cookie, shortlist=None, thumblist=None):
    url = f'https://accounts.booth.pm/orders/{order_num}'
    selectors = {
        'product_div_tag': 'div',
        'product_div_class': 'sheet sheet--p400 mobile:pt-[13px] mobile:px-16 mobile:pb-8',
        'product_info_selector': 'a',
        'product_info_index': 1,
        'thumb_selector': 'img',
        'download_item_selector': (
            'div.legacy-list-item__center, '
            'div.mt-16.desktop\\:flex.desktop\\:justify-between.desktop\\:items-center'
        ),
        'download_link_selector': (
            'div.js-download-button[data-test="downloadable"][data-href*="/downloadables/"], '
            'a.nav-reverse[href*="/downloadables/"]'
        ),
        'filename_selector': (
            'div.min-w-0.u-text-wrap b, '
            'div.min-w-0.break-words.whitespace-pre-line, '
            'div.flex-\\[1\\] b'
        )
    }
    return _crawling_base(url, cookie, selectors, shortlist, thumblist, product_only_filter=product_only)

def crawling_gift(order_num, cookie, shortlist=None, thumblist=None):
    url = f'https://booth.pm/gifts/{order_num}'
    selectors = {
        'product_div_tag': 'div',
        'product_div_class': 'rounded-16 bg-white p-40 mobile:px-16 mobile:pt-24 mobile:pb-40 mobile:rounded-none',
        'product_info_selector': 'div.mt-24.text-left a',
        'thumb_selector': 'img',
        'download_item_selector': 'div.mt-16.desktop\\:flex.desktop\\:justify-between.desktop\\:items-center',
        'download_link_selector': 'div.js-download-button[data-test="downloadable"][data-href*="/downloadables/"]',
        'filename_selector': 'div.min-w-0.break-words.whitespace-pre-line, div.min-w-0.u-text-wrap b'
    }
    return _crawling_base(url, cookie, selectors, shortlist, thumblist)

def download_item(download_number, filepath, cookie):
    url = f'https://booth.pm/downloadables/{download_number}'
    
    response = requests.get(url=url, cookies=cookie, timeout=60)
    response.raise_for_status()
    with open(filepath, "wb") as f:
        f.write(response.content)


def crawling_product(url):
    response = requests.get(url, timeout=30)
    response.raise_for_status()
    html = response.content

    soup = BeautifulSoup(html, "html.parser")
    author_div = soup.find("a", class_="flex gap-4 items-center no-underline preserve-half-leading !text-current typography-16 w-fit")
    # None: private store
    if author_div is None:
        return None

    author_image = author_div.select_one("img")
    if author_image is None:
        return None

    author_image_url = author_image.get("src")
    author_name = author_image.get("alt")
    if not author_image_url or not author_name:
        return None

    return [author_image_url, author_name]
