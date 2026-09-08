import os
import json

DB_FILE = "routes_db.json"

def migrate_db():
    if not os.path.exists(DB_FILE):
        print(f"'{DB_FILE}' 파일을 찾을 수 없습니다.")
        return

    try:
        with open(DB_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        
        count = 0
        # 데이터 구조가 리스트 형태인 경우 (예: [{"route_name": "...", ...}, ...])
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict) and "region" not in item:
                    item["region"] = "gyeonggi"
                    count += 1
                    
        # 데이터 구조가 딕셔너리 형태인 경우 (예: {"routes": [...]})
        elif isinstance(data, dict):
            target_list = None
            for key, value in data.items():
                if isinstance(value, list):
                    target_list = value
                    break
            
            if target_list is not None:
                for item in target_list:
                    if isinstance(item, dict) and "region" not in item:
                        item["region"] = "gyeonggi"
                        count += 1
            else:
                if "region" not in data:
                    data["region"] = "gyeonggi"
                    count += 1

        # 변경된 내용을 다시 routes_db.json에 저장
        with open(DB_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=4)
            
        print(f"성공! '{DB_FILE}' 내의 데이터 총 {count}개 항목에 'gyeonggi' 지역 태그가 일괄 추가되었습니다.")

    except Exception as e:
        print(f"파일 처리 중 오류 발생: {e}")

if __name__ == "__main__":
    migrate_db()