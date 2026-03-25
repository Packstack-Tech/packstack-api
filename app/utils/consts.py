import os

DEVELOPMENT = os.getenv('DEVELOPMENT', 0)

POSTGRES_USER = os.getenv('POSTGRES_USER')
POSTGRES_PASSWORD = os.getenv('POSTGRES_PASSWORD')
POSTGRES_HOST = os.getenv('POSTGRES_HOST')
POSTGRES_DB = os.getenv('POSTGRES_DB')
POSTGRES_PORT = os.getenv('POSTGRES_PORT', '5432')
DATABASE_URL = f"postgresql://{POSTGRES_USER}:{POSTGRES_PASSWORD}@{POSTGRES_HOST}:{POSTGRES_PORT}/{POSTGRES_DB}"

APP_HOST = os.getenv('APP_HOST')
JWT_SECRET = os.getenv('JWT_SECRET')
JWT_ALGORITHM = os.getenv('JWT_ALGORITHM')
RESEND_API_KEY = os.getenv('RESEND_API_KEY')

GOOGLE_CLIENT_IDS = [
    cid.strip() for cid in os.getenv('GOOGLE_CLIENT_IDS', '').split(',') if cid.strip()
]

APPLE_CLIENT_IDS = [
    cid.strip() for cid in os.getenv('APPLE_CLIENT_IDS', '').split(',') if cid.strip()
]

APPLE_KEY_ID = os.getenv('APPLE_KEY_ID')
APPLE_TEAM_ID = os.getenv('APPLE_TEAM_ID')
_raw_apple_key = os.getenv('APPLE_PRIVATE_KEY')
APPLE_PRIVATE_KEY = _raw_apple_key.replace("\\n", "\n") if _raw_apple_key else None

GOOGLE_CLIENT_SECRET = os.getenv('GOOGLE_CLIENT_SECRET')

REVIEW_EMAIL = os.getenv('REVIEW_EMAIL')
REVIEW_OTP = os.getenv('REVIEW_OTP')
