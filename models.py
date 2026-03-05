from sqlalchemy import Column, Integer, Float, String, DateTime, Text
from database import Base
from datetime import datetime

class Budget(Base):
    __tablename__ = "budgets"

    id = Column(Integer, primary_key = True, index=True)
    weekly_limit = Column(Float)
    remaining = Column(Float)

class Transaction(Base):
    __tablename__="transactions"

    id = Column(Integer, primary_key = True, index=True)
    amount = Column(Float)
    store = Column(String)
    category = Column(String, Default = "Uncategorized")
    created_at = Column(DateTime, default = datetime.utcnow)

class GmailToken(Base):
    __tablename__ = "gmail_tokens"
    id = Column(Integer, primary_key=True, index=True)
    token_json = Column(Text)  # store credentials as JSON string
    created_at = Column(DateTime, default=datetime.utcnow)

class ProcessedEmail(Base):
    __tablename__ = "processed_emails"
    id = Column(Integer, primary_key=True, index=True)
    gmail_message_id = Column(String, unique=True, index=True)
    created_at = Column(DateTime, default=datetime.utcnow)