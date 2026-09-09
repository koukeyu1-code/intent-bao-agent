import sys
sys.path.insert(0, 'D:\\意向宝AI大赛\\intent-bao-agent-main\\server')
from app.main import create_app
app = create_app(data_dir='D:\\意向宝AI大赛\\intent-bao-agent-main\\server\\data')
