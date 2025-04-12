from flask import Flask, jsonify, request
from flask_cors import CORS
import mysql.connector
from flask_bcrypt import Bcrypt
import re
import requests
from bs4 import BeautifulSoup
import logging
import os
import json
from dotenv import load_dotenv
import boto3
import datetime
from transformers import pipeline # 요약 라이브러리 (예시)
import openai # 추가된 라이브러리

import logging
logging.basicConfig(level=logging.DEBUG)


print(f"Boto3 version: {boto3.__version__}") # 추가된 코드

app = Flask(__name__)
CORS(app)
bcrypt = Bcrypt(app)

DB_CONFIG = {
    'host': 'localhost',
    'user': 'root',
    'password': 'jonggu2020', # 실제 비밀번호로 변경하세요!
    'database': 'news_db'
}

def get_db_connection():
    return mysql.connector.connect(
        host=DB_CONFIG['host'],
        user=DB_CONFIG['user'],
        password=DB_CONFIG['password'],
        database=DB_CONFIG['database'],
        autocommit=True
    )


logging.basicConfig(level=logging.INFO)

load_dotenv()

HUGGINGFACE_API_TOKEN = os.getenv('HUGGINGFACE_API_TOKEN')
AIML_API_KEY = os.getenv('AIML_API_KEY')
# NLP_CLOUD_API_KEY = os.getenv('NLP_CLOUD_API_KEY') # 더 이상 사용하지 않음

AWS_ACCESS_KEY_ID = os.getenv('AWS_ACCESS_KEY_ID')
AWS_SECRET_ACCESS_KEY = os.getenv('AWS_SECRET_ACCESS_KEY')
AWS_REGION_NAME = os.getenv('AWS_REGION_NAME', 'ap-southeast-2')
S3_OUTPUT_BUCKET_NAME = os.getenv('S3_OUTPUT_BUCKET_NAME')
OPENAI_API_KEY = os.getenv('OPENAI_API_KEY') # OpenAI API 키 로드

openai.api_key = OPENAI_API_KEY # OpenAI API 키 설정

comprehend = boto3.client(
    'comprehend',
    region_name=AWS_REGION_NAME,
    aws_access_key_id=AWS_ACCESS_KEY_ID,
    aws_secret_access_key=AWS_SECRET_ACCESS_KEY
)

s3 = boto3.client(
    's3',
    region_name=AWS_REGION_NAME,
    aws_access_key_id=AWS_ACCESS_KEY_ID,
    aws_secret_access_key=AWS_SECRET_ACCESS_KEY
)

@app.route('/api/news')
def get_news():
    query = request.args.get('query')
    category = request.args.get('category')
    page = request.args.get('page', default=1, type=int)
    limit = request.args.get('limit', default=10, type=int)
    offset = (page - 1) * limit
    try:
        cnx = mysql.connector.connect(**DB_CONFIG)
        cursor = cnx.cursor(dictionary=True)
        fake_reporters_cursor = cnx.cursor()

        fake_reporters_cursor.execute("SELECT reporter_name FROM fake_news_reporters")
        fake_reporter_list = [row[0] for row in fake_reporters_cursor.fetchall()]

        sql_count = "SELECT COUNT(*) FROM articles"
        sql = "SELECT reporter_name, title, link, category, image_url, created, description FROM articles"
        conditions = []
        params = []

        if query:
            search_term = f"%{query}%"
            conditions.append("(title LIKE %s OR reporter_name LIKE %s OR category LIKE %s)")
            params.extend([search_term, search_term, search_term])

        if category:
            conditions.append("category = %s")
            params.append(category)

        if conditions:
            where_clause = " WHERE " + " AND ".join(conditions)
            sql_count += where_clause
            sql += where_clause
        else:
            where_clause = ""

        cursor.execute(sql_count, tuple(params))
        total_news = cursor.fetchone()['COUNT(*)']

        sql += f" LIMIT %s OFFSET %s"
        params.extend([limit, offset])
        cursor.execute(sql, tuple(params))

        news_data = cursor.fetchall()

        for news in news_data:
            if news['reporter_name'] in fake_reporter_list:
                news['is_fake_reporter'] = True
            else:
                news['is_fake_reporter'] = False

        return jsonify({'news': news_data, 'total': total_news})

    except mysql.connector.Error as err:
        return jsonify({'error': str(err)}), 500

    finally:
        if hasattr(cnx, 'is_connected') and cnx.is_connected():
            cursor.close()
            fake_reporters_cursor.close()
            cnx.close()

@app.route('/api/signup', methods=['POST'])
def signup():
    data = request.get_json()
    username = data.get('username')
    password = data.get('password')

    if not username or not password:
        return jsonify({'error': '아이디와 비밀번호를 입력해주세요.'}), 400

    if not re.match(r"[^@]+@[^@]+\.[^@]+", username):
        return jsonify({'error': '아이디 형식이 올바른 이메일 주소가 아닙니다.'}), 400

    try:
        cnx = mysql.connector.connect(**DB_CONFIG)
        cursor = cnx.cursor()

        cursor.execute("SELECT id FROM users WHERE username = %s", (username,))
        existing_user = cursor.fetchone()
        if existing_user:
            return jsonify({'error': '이미 사용 중인 아이디입니다.'}), 409

        hashed_password = bcrypt.generate_password_hash(password).decode('utf-8')

        cursor.execute("INSERT INTO users (username, password) VALUES (%s, %s)", (username, hashed_password))
        cnx.commit()

        return jsonify({'message': '회원가입 성공'}), 201

    except mysql.connector.Error as err:
        cnx.rollback()
        return jsonify({'error': str(err)}), 500

    finally:
        if hasattr(cnx, 'is_connected') and cnx.is_connected():
            cursor.close()
            cnx.close()

@app.route('/api/login', methods=['POST'])
def login():
    data = request.get_json()
    username = data.get('username')
    password = data.get('password')

    if not username or not password:
        return jsonify({'error': '아이디와 비밀번호를 입력해주세요.'}), 400

    try:
        cnx = mysql.connector.connect(**DB_CONFIG)
        cursor = cnx.cursor(dictionary=True)

        cursor.execute("SELECT id, username, password FROM users WHERE username = %s", (username,))
        user = cursor.fetchone()

        if user and bcrypt.check_password_hash(user['password'], password):
            return jsonify({'message': '로그인 성공', 'username': user['username']}), 200
        else:
            return jsonify({'error': '아이디 또는 비밀번호가 일치하지 않습니다.'}), 401

    except mysql.connector.Error as err:
        return jsonify({'error': str(err)}), 500

    finally:
        if hasattr(cnx, 'is_connected') and cnx.is_connected():
            cursor.close()
            cnx.close()

@app.route('/api/fetch-article', methods=['POST'])
def fetch_article():
    data = request.get_json()
    article_link = data.get('link')

    if not article_link:
        return jsonify({'error': '기사 링크를 입력해주세요.'}), 400

    try:
        response = requests.get(article_link)
        response.raise_for_status()
        soup = BeautifulSoup(response.content, 'html.parser')

        body = ""
        reporter_name_in_article = ""
        risk_level = "미확인"
        reporter_mention_count_in_table = 0

        # 기사 본문 추출 (수정)
        article_element = soup.find('article', {'id': 'dic_area', 'class': 'go_trans _article_content'})
        if article_element:
            # article 태그 내의 모든 텍스트를 추출하고, 줄바꿈으로 분리
            text_parts = article_element.get_text(separator='\n').strip().split('\n')
            # 빈 문자열 제거 및 앞뒤 공백 제거
            body_parts = [part.strip() for part in text_parts if part.strip()]
            # 다시 줄바꿈으로 연결
            body = '\n'.join(body_parts)
            logging.info(f"Extracted body (first 100 chars): {body[:100]}")
        else:
            logging.warning("기사 본문을 담는 article 태그를 찾을 수 없습니다.")

        # 기사 기자 이름 추출 코드
        reporter_element = soup.find('em', {'class': 'media_end_head_journalist_name'})
        if reporter_element:
            reporter_name_with_suffix = reporter_element.get_text(strip=True)
            reporter_name_in_article = reporter_name_with_suffix.replace(" 기자", "").strip()
            logging.info(f"Reporter name found in article: {reporter_name_in_article}")
        else:
            logging.warning("기자 이름을 담는 em 태그를 찾을 수 없습니다.")
            reporter_name_in_article = "정보 없음" # 또는 다른 기본값 설정

        if not body.strip():
            logging.warning("추출된 기사 본문이 없습니다.")
            # return jsonify({'error': '기사 분석 중 오류가 발생했습니다: 추출된 내용이 없습니다.'}), 500

        # fake_news_reporters 테이블에서 해당 기자 이름이 등장하는 횟수 세기
        if reporter_name_in_article and reporter_name_in_article != "정보 없음":
            try:
                cnx = mysql.connector.connect(**DB_CONFIG)
                cursor = cnx.cursor()
                cursor.execute("SELECT COUNT(*) FROM fake_news_reporters WHERE reporter_name = %s", (reporter_name_in_article,))
                result = cursor.fetchone()
                if result:
                    reporter_mention_count_in_table = result[0]
                cursor.close()
                cnx.close()

                logging.info(f"Reporter '{reporter_name_in_article}' mentioned {reporter_mention_count_in_table} times in fake_news_reporters table.")

                if reporter_mention_count_in_table >= 5:
                    risk_level = "매우 위험"
                elif reporter_mention_count_in_table == 4:
                    risk_level = "높음"
                elif 2 <= reporter_mention_count_in_table <= 3:
                    risk_level = "보통"
                elif reporter_mention_count_in_table == 1:
                    risk_level = "미약"
                else:
                    risk_level = "안전" # 0번 언급 시 안전

            except mysql.connector.Error as e:
                logging.error(f'데이터베이스 오류: {str(e)}')
                return jsonify({'error': f'데이터베이스 오류: {str(e)}'}), 500
        else:
            risk_level = "기자 정보 없음" # 기사에서 기자 이름을 추출하지 못한 경우

        logging.info(f"Risk level: {risk_level}")

        return jsonify({'article_body': body, 'reporter_risk': {'reporter_name': reporter_name_in_article, 'risk_level': risk_level, 'mention_count_in_table': reporter_mention_count_in_table}}), 200

    except requests.exceptions.RequestException as e:
        logging.error(f'링크를 가져오는 데 실패했습니다: {str(e)}')
        return jsonify({'error': f'기사 분석 중 오류가 발생했습니다: {str(e)}'}), 500
    except Exception as e:
        logging.error(f'기사 분석 중 오류가 발생했습니다: {str(e)}'), 500

@app.route('/api/summarize-article', methods=['POST'])
def summarize_article():
    data = request.get_json()
    article_body = data.get('article_body')
    max_tokens = data.get('max_tokens', 400) # 클라이언트에서 요약 길이를 선택적으로 보낼 수 있도록 함

    if not article_body:
        return jsonify({'error': '요약할 기사 본문이 필요합니다.'}), 400

    try:
        response = openai.chat.completions.create(
            model="gpt-3.5-turbo", # 또는 다른 원하는 모델 선택
            messages=[
                {"role": "system", "content": "You are a helpful assistant that summarizes news articles concisely in Korean."}, # 변경: 한국어로 요약하도록 명시
                {"role": "user", "content": f"Please summarize the following news article in Korean:\n\n{article_body}"}, # 변경: 사용자 프롬프트에도 한국어 명시
            ],
            max_tokens=max_tokens # 요약 길이 조절 파라미터 추가
        )
        summary_text = response.choices[0].message.content
        return jsonify({'summary': summary_text}), 200
    except openai.OpenAIError as e: # 수정된 부분
        logging.error(f"OpenAI API 오류: {e}")
        return jsonify({'error': f"요약 API 오류 발생: {e}"}), 500

@app.route('/api/recommend-article', methods=['POST'])
def recommend_article():
    username = request.headers.get('Authorization') # 사용자 아이디 가져오기
    if not username:
        return jsonify({'error': '로그인이 필요합니다.'}), 401
    data = request.get_json()
    article_link = data.get('article_link')
    article_summary = data.get('article_summary') # 추가: 요약 내용 받기

    if not article_summary:
        return jsonify({'error': '요약 내용이 없습니다.'}), 400

    try:
        cnx = mysql.connector.connect(**DB_CONFIG)
        cursor = cnx.cursor()
        cursor.execute("SELECT id FROM user_article_interactions WHERE user_id = %s AND article_link = %s", (username, article_link))
        existing_vote = cursor.fetchone()
        if existing_vote:
            cursor.close()
            cnx.close()
            return jsonify({'error': '이미 투표표 하셨습니다.'}), 409

        cursor.execute("INSERT INTO user_article_interactions (user_id, article_link, article_summary, vote_type) VALUES (%s, %s, %s, 'recommend')", (username, article_link, article_summary))
        cnx.commit()
        cursor.close()
        cnx.close()
        return jsonify({'message': '추천되었습니다.'}), 200
    except mysql.connector.Error as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/not-recommend-article', methods=['POST'])
def not_recommend_article():
    username = request.headers.get('Authorization') # 사용자 아이디 가져오기
    if not username:
        return jsonify({'error': '로그인이 필요합니다.'}), 401
    data = request.get_json()
    article_link = data.get('article_link')
    article_summary = data.get('article_summary') # 추가: 요약 내용 받기

    if not article_summary:
        return jsonify({'error': '요약 내용이 없습니다.'}), 400

    try:
        cnx = mysql.connector.connect(**DB_CONFIG)
        cursor = cnx.cursor()
        cursor.execute("SELECT id FROM user_article_interactions WHERE user_id = %s AND article_link = %s", (username, article_link))
        existing_vote = cursor.fetchone()
        if existing_vote:
            cursor.close()
            cnx.close()
            return jsonify({'error': '이미 투표하셨습니다.'}), 409

        cursor.execute("INSERT INTO user_article_interactions (user_id, article_link, article_summary, vote_type) VALUES (%s, %s, %s, 'not recommend')", (username, article_link, article_summary))
        cnx.commit()
        cursor.close()
        cnx.close()
        return jsonify({'message': '비추천되었습니다.'}), 200
    except mysql.connector.Error as e:
        return jsonify({'error': str(e)}), 500



#여기부터 수정, 추가
@app.route('/api/get-ranked-news', methods=['GET']) #제목, 페이지, 정렬 때문에 수정(랭킹페이지)
def get_ranked_news():
    time_range = request.args.get('time', 'week')
    page = int(request.args.get('page', 1))
    sort_order = request.args.get('sort', 'recommend')  # 추가된 부분
    page_size = 10
    offset = (page - 1) * page_size

    now = datetime.datetime.now()
    if time_range == 'week':
        start_date = (now - datetime.timedelta(days=now.weekday())).strftime('%Y-%m-%d %H:%M:%S')
        end_date = (now + datetime.timedelta(days=1)).strftime('%Y-%m-%d %H:%M:%S')
    elif time_range == 'month':
        start_date = now.replace(day=1).strftime('%Y-%m-%d %H:%M:%S')
        end_date = now.strftime('%Y-%m-%d %H:%M:%S')
    else:
        return jsonify({'error': 'Invalid time filter'}), 400

    # 정렬 조건 설정
    if sort_order == 'recommend':
        order_by_clause = "recommend_count DESC, not_recommend_count ASC"
    elif sort_order == 'not_recommend':
        order_by_clause = "not_recommend_count DESC, recommend_count ASC"
    else:
        order_by_clause = "recommend_count DESC"

    try:
        conn = mysql.connector.connect(**DB_CONFIG)
        cursor = conn.cursor(dictionary=True)

        # 전체 개수 가져오기
        count_query = """
            SELECT COUNT(DISTINCT uai.article_link) AS total
            FROM user_article_interactions uai
            WHERE uai.created_at >= %s AND uai.created_at < %s
        """
        cursor.execute(count_query, (start_date, end_date))
        total_count = cursor.fetchone()['total']
        total_pages = (total_count + page_size - 1) // page_size

        # 랭킹 데이터 쿼리 (정렬 기준 적용)
        query = f"""
            SELECT
                a.title,
                uai.article_link,
                MAX(uai.article_summary) AS article_summary,
                COUNT(CASE WHEN uai.vote_type = 'recommend' THEN 1 END) AS recommend_count,
                COUNT(CASE WHEN uai.vote_type = 'not recommend' THEN 1 END) AS not_recommend_count,
                (
                    SELECT COUNT(*) 
                    FROM comments c 
                    WHERE c.article_link = uai.article_link AND c.is_deleted = 0
                ) AS comment_count
            FROM user_article_interactions uai
            JOIN articles a ON a.link = uai.article_link
            WHERE uai.created_at >= %s AND uai.created_at < %s
            GROUP BY uai.article_link
            ORDER BY {order_by_clause}
            LIMIT %s OFFSET %s;
        """
        cursor.execute(query, (start_date, end_date, page_size, offset))
        ranked_articles = cursor.fetchall()

        return jsonify({'articles': ranked_articles, 'total_pages': total_pages})

    except mysql.connector.Error as e:
        print("[ERROR] 랭킹 뉴스 에러:", e)
        return jsonify({'error': str(e)}), 500

    finally:
        cursor.close()
        conn.close()


# 기사 링크에 따른 댓글 가져오기(여기부터 추가)
@app.route('/api/comments', methods=['GET'])
def get_comments():
    article_link = request.args.get('article_link')
    if not article_link:
        return jsonify({'error': 'Missing article link'}), 400

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    try:
        cursor.execute(
             "SELECT * FROM comments WHERE article_link = %s ORDER BY created_at ASC",
            (article_link,)
        )
        comments = cursor.fetchall()

        # 🔹 created_at을 문자열로 포맷
        for comment in comments:
            created_at = comment.get('created_at')
            if isinstance(created_at, datetime.datetime):  
                comment['created_at'] = created_at.strftime('%Y-%m-%d %H:%M:%S')

    #디버그 코드
        return jsonify({'comments': comments}), 200

    except Exception as e:
        print(f"[ERROR] 댓글 불러오기 실패: {e}")
        return jsonify({'error': '댓글 불러오기 실패'}), 500

    finally:
        cursor.close()
        conn.close()

# 댓글 작성하기
@app.route('/api/comments', methods=['POST'])
def post_comment():
    data = request.get_json()
    article_link = data.get('article_link')
    username = data.get('username')
    content = data.get('content')
    parent_id = data.get('parent_id')  # 대댓글이면 부모 ID 전달

    if not article_link or not username or not content:
        return jsonify({'error': 'Missing fields'}), 400

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    try:
        now = datetime.datetime.now()
        insert_query = """
            INSERT INTO comments (article_link, username, content, created_at, parent_id)
            VALUES (%s, %s, %s, %s, %s)
        """
        cursor.execute(insert_query, (article_link, username, content, now, parent_id))
        conn.commit()

        comment_id = cursor.lastrowid
        new_comment = {
            'id': comment_id,
            'article_link': article_link,
            'username': username,
            'content': content,
            'created_at': now.isoformat(),
            'parent_id': parent_id
        }

    #디버그 코드
        return jsonify({'comment': new_comment}), 201
    except Exception as e:
        print(f"[ERROR] 댓글 작성 실패: {e}")
        return jsonify({'error': '댓글 작성 실패'}), 500
    finally:
        cursor.close()
        conn.close()

#댓글 수정하기
@app.route('/api/comments/<int:comment_id>', methods=['PUT'])
def update_comment(comment_id):
    data = request.get_json()
    new_content = data.get('content')

    if not new_content:
        return jsonify({'error': '수정할 내용이 없습니다.'}), 400

    conn = get_db_connection()
    cursor = conn.cursor()

    try:
        cursor.execute("""
            UPDATE comments 
            SET content = %s, updated_at = NOW() 
            WHERE id = %s AND is_deleted = FALSE
        """, (new_content, comment_id))
        conn.commit()
        return jsonify({'message': '댓글이 수정되었습니다.'})
    except Exception as e:
        print(f"[ERROR] 댓글 수정 실패: {e}")
        return jsonify({'error': '댓글 수정 실패'}), 500
    finally:
        cursor.close()
        conn.close()

#댓글 삭제하기
@app.route('/api/comments/<int:comment_id>', methods=['DELETE'])
def delete_comment(comment_id):
    conn = get_db_connection()
    cursor = conn.cursor()

    try:
        cursor.execute("""
            UPDATE comments 
            SET is_deleted = TRUE, content = '삭제된 댓글입니다.' 
            WHERE id = %s
        """, (comment_id,))
        conn.commit()
        return jsonify({'message': '댓글이 삭제되었습니다.'})
    except Exception as e:
        print(f"[ERROR] 댓글 삭제 실패: {e}")
        return jsonify({'error': '댓글 삭제 실패'}), 500
    finally:
        cursor.close()
        conn.close()

# 댓글 추천/비추천 등록
@app.route('/api/comments/vote', methods=['POST'])
def vote_comment():
    data = request.get_json()
    comment_id = data.get('comment_id')
    username = data.get('username')
    is_upvote = data.get('is_upvote')

    if not comment_id or not username or is_upvote is None:
        return jsonify({'error': '필수 정보가 누락되었습니다.'}), 400

    conn = get_db_connection()
    cursor = conn.cursor()

    try:
        # 이미 투표했는지 확인
        cursor.execute("""
            SELECT * FROM comment_votes
            WHERE comment_id = %s AND username = %s
        """, (comment_id, username))
        existing = cursor.fetchone()
        if existing:
            return jsonify({'error': '이미 추천 또는 비추천하였습니다.'}), 409

        vote_type = 'up' if is_upvote else 'down'
        cursor.execute("""
            INSERT INTO comment_votes (comment_id, username, vote_type)
            VALUES (%s, %s, %s)
        """, (comment_id, username, vote_type))
        conn.commit()
        return jsonify({'message': '투표 성공'})
    except Exception as e:
        print(f"[ERROR] 투표 실패: {e}")
        return jsonify({'error': '투표 실패'}), 500
    finally:
        cursor.close()
        conn.close()

# 댓글 추천/비추천 수 반환
@app.route('/api/comments/vote-counts')
def get_vote_counts():
    comment_id = request.args.get('comment_id')
    if not comment_id:
        return jsonify({'error': '댓글 ID가 누락되었습니다.'}), 400

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    try:
        cursor.execute("""
            SELECT
                SUM(CASE WHEN vote_type = 'up' THEN 1 ELSE 0 END) AS upvotes,
                SUM(CASE WHEN vote_type = 'down' THEN 1 ELSE 0 END) AS downvotes
            FROM comment_votes
            WHERE comment_id = %s
        """, (comment_id,))
        result = cursor.fetchone()
        return jsonify({'counts': result})
    except Exception as e:
        print(f"[ERROR] 투표 수 조회 실패: {e}")
        return jsonify({'error': '투표 수 조회 실패'}), 500
    finally:
        cursor.close()
        conn.close()


if __name__ == '__main__':
    app.run(debug=True)
