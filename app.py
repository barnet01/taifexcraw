"""
台灣期交所選擇權數據爬蟲 API
"""
from flask import Flask, request, jsonify, render_template
from flask_cors import CORS
import requests
from bs4 import BeautifulSoup
import re
from datetime import datetime

app = Flask(__name__)
CORS(app, origins='*', supports_credentials=False)

# 台灣期交所 URL
TAIFEX_URL = "https://www.taifex.com.tw/cht/3/callsAndPutsDate"
TAIFEX_FUT_URL = "https://www.taifex.com.tw/cht/3/futContractsDate"


def extract_fields(values):
    keys = ['buy_volume', 'buy_amount', 'sell_volume', 'sell_amount',
            'net_volume', 'net_amount', 'oi_buy_volume', 'oi_buy_amount',
            'oi_sell_volume', 'oi_sell_amount', 'oi_net_volume', 'oi_net_amount']
    return {k: (values[i] if i < len(values) else '') for i, k in enumerate(keys)}


def parse_taifex_data(html_content):
    soup = BeautifulSoup(html_content, 'html.parser')
    table = soup.find('table', class_='table_f')
    if not table:
        tables = soup.find_all('table')
        table = max(tables, key=lambda t: len(t.find_all('tr')), default=None) if tables else None
    if not table:
        return None
    rows = table.find_all('tr')
    data = {
        'date': '',
        'txo': {
            'call': {'dealer': {}, 'foreign': {}},
            'put':  {'dealer': {}, 'foreign': {}}
        }
    }
    m = re.search(r'(\d{4}/\d{2}/\d{2})', soup.get_text())
    if m:
        data['date'] = m.group(1)

    in_txo = False
    current_option_type = None

    for row in rows:
        cells = row.find_all(['td', 'th'])
        cell_texts = [c.get_text(strip=True) for c in cells]
        if len(cell_texts) < 3:
            continue
        if in_txo and any(n in cell_texts for n in ['電子選擇權', '金融選擇權']):
            break
        if '臺指選擇權' in cell_texts:
            in_txo = True
            idx = cell_texts.index('臺指選擇權')
            if idx + 1 < len(cell_texts):
                opt = cell_texts[idx + 1]
                if opt == '買權':
                    current_option_type = 'call'
                elif opt == '賣權':
                    current_option_type = 'put'
            if idx + 2 < len(cell_texts):
                identity = cell_texts[idx + 2]
                values = cell_texts[idx + 3:]
                if current_option_type:
                    if identity == '自營商':
                        data['txo'][current_option_type]['dealer'] = extract_fields(values)
                    elif identity in ('外資', '外資及陸資'):
                        data['txo'][current_option_type]['foreign'] = extract_fields(values)
            continue
        if not in_txo:
            continue
        first = cell_texts[0]
        if first == '賣權':
            current_option_type = 'put'
            if len(cell_texts) >= 2:
                identity = cell_texts[1]
                values = cell_texts[2:]
                if identity == '自營商':
                    data['txo']['put']['dealer'] = extract_fields(values)
                elif identity in ('外資', '外資及陸資'):
                    data['txo']['put']['foreign'] = extract_fields(values)
            continue
        if first in ('外資', '外資及陸資') and current_option_type:
            data['txo'][current_option_type]['foreign'] = extract_fields(cell_texts[1:])
            continue
        if first == '自營商' and current_option_type:
            data['txo'][current_option_type]['dealer'] = extract_fields(cell_texts[1:])
            continue
    return data


MULTIPLIER = {'臺股期貨': 200, '小型臺指期貨': 50, '微型臺指期貨': 10}
FUTURES_TARGETS = list(MULTIPLIER.keys())


def parse_futures_data(html_content):
    """
    解析期交所三大法人期貨數據。
    自營商行 cells=15: [序號, 商品名稱, 身份別, 多方口數, 多方金額, 空方口數, 空方金額,
                        淨額口數, 淨額金額, OI多方口數, OI多方金額, OI空方口數, OI空方金額, OI淨額口數, OI淨額金額]
    投信/外資行 cells=13: [身份別, 多方口數, 多方金額, 空方口數, 空方金額,
                           淨額口數, 淨額金額, OI多方口數, OI多方金額, OI空方口數, OI空方金額, OI淨額口數, OI淨額金額]
    數字欄(去掉身份別前): [4]=淨額口數 [5]=淨額金額 [10]=OI淨額口數 [11]=OI淨額金額
    """
    soup = BeautifulSoup(html_content, 'html.parser')
    table = soup.find('table', class_='table_f')
    if not table:
        tables = soup.find_all('table')
        table = max(tables, key=lambda t: len(t.find_all('tr')), default=None) if tables else None
    if not table:
        return None

    rows = table.find_all('tr')
    date_str = ''
    dm = re.search(r'(\d{4}/\d{2}/\d{2})', soup.get_text())
    if dm:
        date_str = dm.group(1)

    result = {'date': date_str, 'futures': {n: {'dealer': {}, 'foreign': {}} for n in FUTURES_TARGETS}}
    current_product = None

    for row in rows:
        cells = row.find_all(['td', 'th'])
        texts = [c.get_text(strip=True) for c in cells]
        n = len(texts)

        # 停止條件：遇到小計/合計行
        if any(t in ('期貨小計', '期貨合計') for t in texts):
            break

        # cells=15：新商品行（含序號+商品名+身份別+12個數字）
        # 無論是否為目標商品都要更新 current_product，
        # 非目標商品設 None 以隔離後續外資/投信行
        if n == 15:
            current_product = texts[1] if texts[1] in FUTURES_TARGETS else None
            if current_product and texts[2] == '自營商':
                vals = texts[3:]  # 12個數字
                result['futures'][current_product]['dealer'] = {
                    'net_volume':    vals[4],
                    'net_amount':    vals[5],
                    'oi_net_volume': vals[10],
                    'oi_net_amount': vals[11],
                }
            continue

        # cells=13：同商品的投信/外資行（身份別+12個數字）
        if n == 13 and current_product:
            if texts[0] in ('外資', '外資及陸資'):
                vals = texts[1:]  # 12個數字
                result['futures'][current_product]['foreign'] = {
                    'net_volume':    vals[4],
                    'net_amount':    vals[5],
                    'oi_net_volume': vals[10],
                    'oi_net_amount': vals[11],
                }
            # 投信略過
            continue

    return result



def index():
    """主頁面"""
    return render_template('index.html')


@app.route('/api/query', methods=['POST'])
def query_data():
    """查詢選擇權數據 API"""
    try:
        # 獲取請求數據
        req_data = request.get_json()
        query_date = req_data.get('date', '')
        
        if not query_date:
            return jsonify({
                'success': False,
                'error': '請提供日期參數'
            }), 400
        
        # 驗證日期格式
        try:
            datetime.strptime(query_date, '%Y/%m/%d')
        except ValueError:
            return jsonify({
                'success': False,
                'error': '日期格式錯誤，請使用 YYYY/MM/DD 格式'
            }), 400
        
        # 發送請求到期交所網站
        session = requests.Session()
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
            'Content-Type': 'application/x-www-form-urlencoded'
        }
        
        form_data = {
            'queryType': '1',
            'goDay': '',
            'doQuery': '1',
            'dateaddcnt': '',
            'queryDate': query_date,
            'commodityId': 'TXO'  # 臺指選擇權
        }
        
        response = session.post(TAIFEX_URL, data=form_data, headers=headers, timeout=30)
        
        if response.status_code != 200:
            return jsonify({
                'success': False,
                'error': f'無法連接到期交所網站 (狀態碼: {response.status_code})'
            }), 500
        
        # 解析數據
        data = parse_taifex_data(response.text)
        
        txo = data.get('txo', {}) if data else {}
        has_data = (txo.get('call', {}).get('dealer') or txo.get('call', {}).get('foreign') or
                    txo.get('put', {}).get('dealer') or txo.get('put', {}).get('foreign'))

        
        return jsonify({
            'success': True,
            'data': data
        })
        
    except requests.RequestException as e:
        return jsonify({
            'success': False,
            'error': f'網路請求錯誤: {str(e)}'
        }), 500
    except Exception as e:
        return jsonify({
            'success': False,
            'error': f'伺服器錯誤: {str(e)}'
        }), 500



@app.route('/api/query_futures', methods=['POST'])
def query_futures():
    try:
        req_data = request.get_json()
        query_date = req_data.get('date', '')
        if not query_date:
            return jsonify({'success': False, 'error': '請提供日期參數'}), 400
        try:
            datetime.strptime(query_date, '%Y/%m/%d')
        except ValueError:
            return jsonify({'success': False, 'error': '日期格式錯誤，請使用 YYYY/MM/DD 格式'}), 400

        session = requests.Session()
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Content-Type': 'application/x-www-form-urlencoded'
        }
        form_data = {
            'queryType': '1', 'goDay': '', 'doQuery': '1',
            'dateaddcnt': '', 'queryDate': query_date, 'commodityId': '',  # 空字串以抓取所有商品
        }
        response = session.post(TAIFEX_FUT_URL, data=form_data, headers=headers, timeout=30)

        if response.status_code != 200:
            return jsonify({'success': False, 'error': f'無法連接到期交所網站 (狀態碼: {response.status_code})'}), 500

        data = parse_futures_data(response.text)
        futures = data.get('futures', {}) if data else {}
        has_data = any(
            futures.get(p, {}).get('dealer') or futures.get(p, {}).get('foreign')
            for p in FUTURES_TARGETS
        )
        if not has_data:
            return jsonify({'success': False, 'error': '找不到該日期的期貨數據，請確認日期是否為交易日'}), 404

        return jsonify({'success': True, 'data': data})

    except requests.RequestException as e:
        return jsonify({'success': False, 'error': f'網路請求錯誤: {str(e)}'}), 500
    except Exception as e:
        return jsonify({'success': False, 'error': f'伺服器錯誤: {str(e)}'}), 500

@app.route('/api/test', methods=['GET'])
def test_connection():
    """測試 API 連接"""
    return jsonify({
        'success': True,
        'message': 'API 服務運行正常'
    })


if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)