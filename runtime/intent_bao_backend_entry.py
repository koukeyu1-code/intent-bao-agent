import sys
sys.path.insert(0, 'E:\\intent-bao-agent\\server')
from app.main import create_app
app = create_app(data_dir='E:\\intent-bao-agent\\server\\data')
