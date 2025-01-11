import logging
import sys
import os
import socket
import threading
from contextlib import closing
from datetime import datetime, timedelta, timezone
from collections import Counter
import io
import tempfile
import requests
import time
from requests.exceptions import RequestException
from flask import Flask, render_template, request, send_file, jsonify, current_app
from google_play_scraper import Sort, reviews
from app_store_scraper import AppStore
import pandas as pd
import nltk
from nltk.corpus import stopwords
from nltk.tokenize import word_tokenize
from textblob import TextBlob
from openai import OpenAI
from openai import RateLimitError, APIError
from datetime import datetime, timedelta, timezone
from flask_sqlalchemy import SQLAlchemy
import csv
from dateutil import parser

# 配置日志
logging.basicConfig(level=logging.DEBUG, stream=sys.stdout,
                    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

logger.debug("应用程序启动")

# 设置OpenAI API密钥
os.environ["OPENAI_API_KEY"] = "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"

# 创建OpenAI客端
client = OpenAI()

nltk.download('punkt', quiet=True)
nltk.download('stopwords', quiet=True)

app = Flask(__name__)

# 全局变量
scraping_progress = {}
reviews_data = None  # 用于存储抓取的评论数据
scraping_lock = threading.Lock()

app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///reviews.db'
db = SQLAlchemy(app)

with app.app_context():
    db.create_all()

class Review(db.Model):
    __tablename__ = 'review'  # 显式指定表名为小写
    id = db.Column(db.Integer, primary_key=True)
    reviewId = db.Column(db.String(100))
    userName = db.Column(db.String(100))
    content = db.Column(db.Text)
    score = db.Column(db.Float)
    at = db.Column(db.DateTime)

def get_country_code(language):
    language_to_country = {
        'en': 'us', 'de': 'de', 'es': 'es', 'fr': 'fr', 'it': 'it',
        'ja': 'jp', 'ko': 'kr', 'pt': 'pt', 'ru': 'ru', 'zh': 'cn'
    }
    return language_to_country.get(language.lower(), language.lower())

def get_store_code(store):
    return 'google' if store.lower() == 'google play' else 'apple'

def fetch_reviews_batch(package_name, lang, country, sort, count, filter_score_with, continuation_token):
    logger.debug(f"请求评论，包名: {package_name}, 言: {lang}, 家: {country}, 排序: {sort}, 数量: {count}, 续传令牌: {continuation_token}")
    try:
        result, token = reviews(
            package_name, lang=lang, country=country, sort=sort,
            count=count, filter_score_with=filter_score_with,
            continuation_token=continuation_token
        )
        logger.debug(f"返回的评论数量: {len(result)}")
        return result, token
    except Exception as e:
        logger.error(f"抓取评论时发生错误: {str(e)}")
        return [], None

def fetch_apple_reviews(app_id, country, start_date, end_date):
    url = f"https://itunes.apple.com/{country}/rss/customerreviews/page=1/id={app_id}/sortBy=mostRecent/json"
    response = requests.get(url)
    response.raise_for_status()
    data = response.json()
    reviews = []
    for entry in data['feed']['entry'][1:]:  # 跳过第一个条目，因为它通常是应用信息
        review_date = parser.parse(entry['updated']['label']).replace(tzinfo=timezone.utc)
        if start_date <= review_date <= end_date:
            reviews.append({
                'review_id': entry['id']['label'],
                'userName': entry['author']['name']['label'],
                'review': entry['content']['label'],
                'rating': int(entry['im:rating']['label']),
                'date': review_date.strftime("%Y-%m-%dT%H:%M:%SZ")
            })
    return reviews

def scrape_reviews(app_id, language, store, start_date, end_date):
    try:
        start_date = start_date.replace(tzinfo=timezone.utc) if start_date.tzinfo is None else start_date
        end_date = end_date.replace(tzinfo=timezone.utc) if end_date.tzinfo is None else end_date
        
        if store.lower() == 'google play':
            result, _ = reviews(
                app_id,
                lang=language,
                country=get_country_code(language),
                sort=Sort.NEWEST,
                count=2000
            )
            all_reviews = [
                {
                    'reviewId': review['reviewId'],
                    'userName': review['userName'],
                    'content': review['content'],
                    'score': review['score'],
                    'at': review['at'].replace(tzinfo=timezone.utc) if review['at'].tzinfo is None else review['at']
                }
                for review in result
                if start_date <= (review['at'].replace(tzinfo=timezone.utc) if review['at'].tzinfo is None else review['at']) <= end_date
            ]
        elif store.lower() == 'app store':
            country = get_country_code(language)
            apple_reviews = fetch_apple_reviews(app_id, country, start_date, end_date)
            all_reviews = [
                {
                    'reviewId': review.get('review_id', ''),
                    'userName': review.get('userName', ''),
                    'content': review.get('review', ''),
                    'score': review.get('rating', 0),
                    'at': datetime.strptime(review.get('date', ''), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc),
                }
                for review in apple_reviews
                if review.get('date') and datetime.strptime(review['date'], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc) >= start_date
            ]
        else:
            raise ValueError("Unsupported store")

        return all_reviews[:2000]  # 限制最多返回2000条评论

    except Exception as e:
        logger.error(f"抓取过程中发生错误: {str(e)}")
        logger.exception("详细错误信息")
        return []

def analyze_with_gpt(reviews, analysis_type):
    global client
    try:
        prompt = f"分析以下用户评论的{analysis_type}：\n\n"
        for review in reviews[:5]:  # 只使用5条评论以控制token数量
            prompt += f"- {review['content']}\n"
        prompt += f"\n请提供关于{analysis_type}的简洁分析："

        response = client.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[
                {"role": "system", "content": "你是一个专数据分析师，擅长分析用户评论。"},
                {"role": "user", "content": prompt}
            ]
        )
        
        return response.choices[0].message.content.strip()
    except (RateLimitError, APIError) as e:
        print(f"OpenAI API error: {str(e)}")
        return fallback_analysis(reviews, analysis_type)

def fallback_analysis(reviews, analysis_type):
    all_text = ' '.join([review['content'] for review in reviews])
    words = word_tokenize(all_text.lower())
    stop_words = set(stopwords.words('english'))
    filtered_words = [word for word in words if word.isalnum() and word not in stop_words]
    word_freq = Counter(filtered_words)
    
    if analysis_type == "主要用例":
        return ', '.join([word for word, _ in word_freq.most_common(5)])
    elif analysis_type == "解决的问题":
        positive_words = [word for word, count in word_freq.items() if TextBlob(word).sentiment.polarity > 0]
        return ', '.join(positive_words[:5])
    elif analysis_type == "用户不满意的点":
        negative_words = [word for word, count in word_freq.items() if TextBlob(word).sentiment.polarity < 0]
        return ', '.join(negative_words[:5])
    else:
        return "无法分析"

def analyze_reviews(reviews_data):
    primary_use_case = analyze_with_gpt(reviews_data, "主要用例")
    solved_problem = analyze_with_gpt(reviews_data, "解决的问题")
    dissatisfaction_points = analyze_with_gpt(reviews_data, "用户不满意的点")
    
    return {
        "primary_use_case": primary_use_case,
        "solved_problem": solved_problem,
        "dissatisfaction_points": dissatisfaction_points
    }

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/scrape_and_download', methods=['POST'])
def scrape_and_download():
    try:
        data = request.json
        app_id = data['app_id']
        language = data['language']
        store = data['store']
        
        end_date = datetime.now(timezone.utc)
        start_date = end_date - timedelta(days=90)
        
        if data['start_date']:
            start_date = datetime.strptime(data['start_date'], '%Y-%m-%d').replace(tzinfo=timezone.utc)
        if data['end_date']:
            end_date = datetime.strptime(data['end_date'], '%Y-%m-%d').replace(tzinfo=timezone.utc) + timedelta(days=1)
        
        reviews = scrape_reviews(app_id, language, store, start_date, end_date)
        
        if reviews:
            output = io.StringIO()
            writer = csv.DictWriter(output, fieldnames=['reviewId', 'userName', 'content', 'score', 'at'])
            writer.writeheader()
            for review in reviews:
                writer.writerow(review)
            
            output.seek(0)
            
            return send_file(
                io.BytesIO(output.getvalue().encode()),
                mimetype='text/csv',
                as_attachment=True,
                download_name='reviews.csv'
            )
        else:
            error_message = f"在所选的语言（{language}）和商店（{store}）中没有找到评论。请尝试其他语言或检查应用 ID 是否正确。"
            return jsonify({"error": error_message, "status": "no_reviews"}), 404
    except Exception as e:
        return jsonify({"error": str(e), "status": "error"}), 500

@app.route('/port')
def get_port():
    return jsonify({'port': request.host.split(':')[-1]})

def find_free_port(start_port=5000, max_port=65535):
    for port in range(start_port, max_port + 1):
        with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as sock:
            if sock.connect_ex(('localhost', port)) != 0:
                return port
    raise RuntimeError("无法找到可用的端口")

if __name__ == '__main__':
    # 初始化全局变量
    scraping_progress = {"status": "not_started", "total_reviews": 0}
    analysis_result = None

    try:
        port = 5006  # 首选端口
        logger.info(f"尝试端口 {port} 上启动应用")
        app.run(debug=True, port=port)
    except OSError as e:
        if "Address already in use" in str(e):
            new_port = find_free_port(start_port=5000)
            logger.warning(f"端 {port} 已被占用，正在切换到端口 {new_port}")
            app.run(debug=True, port=new_port)
        else:
            logger.error(f"启动应用时发生错误: {str(e)}")
            raise
