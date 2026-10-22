from dotenv import load_dotenv

load_dotenv()  # 모듈들이 import 시점에 SYSTEM_TOKEN·BACKEND_URL을 읽으므로 패키지에서 먼저 읽는다. 이미 있는 환경변수가 우선
