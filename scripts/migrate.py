from app.db.base import init_db

if __name__ == "__main__":
    init_db()
    print("数据库迁移已完成")
