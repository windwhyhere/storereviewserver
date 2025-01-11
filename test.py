from google_play_scraper import Sort, reviews_all

print("脚本开始执行")

try:
    print("准备抓取评论...")
    result = reviews_all(
        'com.fantome.penguinisle',  # 另一个应用的包名
        lang='en',
        country='us',
        sort=Sort.NEWEST,
        filter_score_with=None
    )
    print(f"抓取到的评论数量: {len(result)}")
    for review in result:
        print(review)
except Exception as e:
    print(f"抓取过程中发生错误: {str(e)}")
    import traceback
    traceback.print_exc()