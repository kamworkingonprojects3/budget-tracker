from sqlalchemy import Column, Integer, Float, String, DateTime, Text
from database import Base
from datetime import datetime


class Budget(Base):
    __tablename__ = "budgets"

    id = Column(Integer, primary_key=True, index=True)

    weekly_limit = Column(Float, nullable=False)
    remaining = Column(Float, nullable=False)


class Transaction(Base):
    __tablename__ = "transactions"

    id = Column(Integer, primary_key=True, index=True)

    amount = Column(Float, nullable=False)
    store = Column(String(200), nullable=False)

    category = Column(String(100), default="Uncategorized")

    created_at = Column(
        DateTime,
        default=datetime.utcnow,
        index=True
    )


class GmailToken(Base):
    __tablename__ = "gmail_tokens"

    id = Column(Integer, primary_key=True, index=True)

    token_json = Column(Text, nullable=False)

    created_at = Column(
        DateTime,
        default=datetime.utcnow,
        index=True
    )


class ProcessedEmail(Base):
    __tablename__ = "processed_emails"

    id = Column(Integer, primary_key=True, index=True)

    gmail_message_id = Column(
        String(255),
        unique=True,
        index=True,
        nullable=False
    )

    created_at = Column(
        DateTime,
        default=datetime.utcnow,
        index=True
    )