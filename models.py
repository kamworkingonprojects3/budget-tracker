from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import relationship

from database import Base


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    email = Column(String(255), unique=True, index=True, nullable=False)
    password_hash = Column(String(255), nullable=False)
    premium = Column(Boolean, default=False, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    budgets = relationship("Budget", back_populates="user", cascade="all, delete-orphan")
    transactions = relationship("Transaction", back_populates="user", cascade="all, delete-orphan")
    gmail_tokens = relationship("GmailToken", back_populates="user", cascade="all, delete-orphan")
    processed_emails = relationship("ProcessedEmail", back_populates="user", cascade="all, delete-orphan")


class Budget(Base):
    __tablename__ = "budgets"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), index=True, nullable=False)
    weekly_limit = Column(Float, nullable=False)
    remaining = Column(Float, nullable=False)

    user = relationship("User", back_populates="budgets")


class Transaction(Base):
    __tablename__ = "transactions"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), index=True, nullable=False)
    amount = Column(Float, nullable=False)
    store = Column(String(200), nullable=False)
    category = Column(String(100), default="Uncategorized", nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, index=True, nullable=False)

    user = relationship("User", back_populates="transactions")


class GmailToken(Base):
    __tablename__ = "gmail_tokens"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), index=True, nullable=False)
    token_json = Column(Text, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, index=True, nullable=False)

    user = relationship("User", back_populates="gmail_tokens")


class ProcessedEmail(Base):
    __tablename__ = "processed_emails"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), index=True, nullable=False)
    gmail_message_id = Column(String(255), index=True, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, index=True, nullable=False)

    user = relationship("User", back_populates="processed_emails")