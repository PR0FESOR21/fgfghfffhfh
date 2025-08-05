from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from motor.motor_asyncio import AsyncIOMotorClient
from datetime import datetime
from typing import Optional
import secrets
import string
import random
import logging
import os
from contextlib import asynccontextmanager

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Database connection
class Database:
    client: Optional[AsyncIOMotorClient] = None
    database = None

db = Database()

# Pydantic models
class WalletRequest(BaseModel):
    wallet_address: str = Field(..., min_length=20, max_length=100, description="Wallet address")
    referred_by: Optional[str] = Field(None, description="Referral code of the person who invited")

class WalletResponse(BaseModel):
    success: bool
    message: str
    referral_code: str
    wallet_address: str
    referred_by: Optional[str] = None

# Helper functions
def generate_referral_code(length: int = 6) -> str:
    """Generate a random referral code"""
    characters = string.ascii_uppercase + string.digits
    return ''.join(random.choices(characters, k=length))

async def get_unique_referral_code() -> str:
    """Generate unique referral code with collision checking"""
    referral_code_candidate = generate_referral_code()
    counter = 0
    
    while True:
        # Check if referral code already exists
        existing = await db.database.wallets.find_one({"referral_code": referral_code_candidate})
        if not existing:
            break
            
        counter += 1
        referral_code_candidate = generate_referral_code(length=7 if counter < 5 else 8)
        
        if counter > 10:
            logger.error(f"Could not generate unique referral code after {counter} attempts.")
            return f"REF{secrets.token_hex(5).upper()}"
    
    return referral_code_candidate

# Database functions
async def connect_to_mongo():
    """Create database connection"""
    try:
        # Get MongoDB URL from environment or use default
        mongodb_url = os.getenv("MONGODB_URL", "mongodb+srv://profesor:root@cluster0.6kpph5n.mongodb.net/")
        db_name = os.getenv("MONGODB_DB_NAME", "wallet_app")
        
        db.client = AsyncIOMotorClient(mongodb_url)
        db.database = db.client[db_name]
        
        # Test connection
        await db.client.admin.command('ping')
        logger.info(f"Successfully connected to MongoDB: {db_name}")
    except Exception as e:
        logger.error(f"Failed to connect to MongoDB: {e}")
        raise

async def close_mongo_connection():
    """Close database connection"""
    if db.client:
        db.client.close()
        logger.info("MongoDB connection closed")

# Lifespan manager
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    await connect_to_mongo()
    yield
    # Shutdown
    await close_mongo_connection()

# FastAPI app
app = FastAPI(
    title="Wallet Registration API",
    description="Simple API to register wallet and get referral code",
    version="1.0.0",
    lifespan=lifespan
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# Routes
@app.get("/")
async def root():
    return {
        "message": "Wallet Registration API", 
        "status": "running",
        "endpoints": {
            "register": "POST /register",
            "health": "GET /health"
        }
    }

@app.post("/register", response_model=WalletResponse)
async def register_wallet(wallet_data: WalletRequest):
    """Register wallet address and return referral code"""
    try:
        # Check if wallet already exists
        existing_wallet = await db.database.wallets.find_one(
            {"wallet_address": wallet_data.wallet_address}
        )
        
        if existing_wallet:
            logger.info(f"Wallet already exists: {wallet_data.wallet_address}")
            return WalletResponse(
                success=True,
                message="Wallet already registered",
                referral_code=existing_wallet.get("referral_code", ""),
                wallet_address=wallet_data.wallet_address,
                referred_by=existing_wallet.get("referred_by")
            )
        
        # Validate referral code if provided
        referrer_wallet = None
        if wallet_data.referred_by:
            referrer_wallet = await db.database.wallets.find_one(
                {"referral_code": wallet_data.referred_by}
            )
            
            if not referrer_wallet:
                logger.warning(f"Invalid referral code: {wallet_data.referred_by}")
                return WalletResponse(
                    success=False,
                    message="Invalid referral code",
                    referral_code="",
                    wallet_address=wallet_data.wallet_address
                )
            
            # Check if trying to refer themselves
            if referrer_wallet["wallet_address"] == wallet_data.wallet_address:
                logger.warning(f"Self-referral attempt: {wallet_data.wallet_address}")
                return WalletResponse(
                    success=False,
                    message="Cannot refer yourself",
                    referral_code="",
                    wallet_address=wallet_data.wallet_address
                )
        
        # Generate unique referral code
        referral_code = await get_unique_referral_code()
        
        # Create wallet document
        wallet_doc = {
            "wallet_address": wallet_data.wallet_address,
            "referral_code": referral_code,
            "referred_by": wallet_data.referred_by,
            "referred_by_wallet": referrer_wallet["wallet_address"] if referrer_wallet else None,
            "created_at": datetime.utcnow()
        }
        
        # Insert to database
        result = await db.database.wallets.insert_one(wallet_doc)
        
        if result.inserted_id:
            # Update referrer's referral count
            if referrer_wallet:
                await db.database.wallets.update_one(
                    {"_id": referrer_wallet["_id"]},
                    {"$inc": {"referral_count": 1}}
                )
                logger.info(f"Updated referral count for: {referrer_wallet['wallet_address']}")
            
            logger.info(f"New wallet registered: {wallet_data.wallet_address} -> {referral_code}" + 
                       (f" (referred by {wallet_data.referred_by})" if wallet_data.referred_by else ""))
            
            return WalletResponse(
                success=True,
                message="Wallet registered successfully",
                referral_code=referral_code,
                wallet_address=wallet_data.wallet_address,
                referred_by=wallet_data.referred_by
            )
        else:
            logger.error(f"Failed to insert wallet: {wallet_data.wallet_address}")
            return WalletResponse(
                success=False,
                message="Failed to register wallet",
                referral_code="",
                wallet_address=wallet_data.wallet_address
            )
            
    except Exception as e:
        logger.error(f"Error registering wallet {wallet_data.wallet_address}: {e}")
        return WalletResponse(
            success=False,
            message="Internal server error",
            referral_code="",
            wallet_address=wallet_data.wallet_address
        )

@app.get("/health")
async def health_check():
    """Health check endpoint"""
    try:
        # Test database connection
        await db.client.admin.command('ping')
        
        # Count registered wallets
        wallet_count = await db.database.wallets.count_documents({})
        
        return {
            "status": "healthy",
            "database": "connected",
            "registered_wallets": wallet_count,
            "timestamp": datetime.utcnow()
        }
    except Exception as e:
        logger.error(f"Health check failed: {e}")
        return {
            "status": "unhealthy",
            "database": "disconnected",
            "error": str(e),
            "timestamp": datetime.utcnow()
        }

@app.get("/stats")
async def get_stats():
    """Get registration statistics"""
    try:
        total_wallets = await db.database.wallets.count_documents({})
        
        # Get recent registrations (last 24 hours)
        yesterday = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
        recent_count = await db.database.wallets.count_documents({
            "created_at": {"$gte": yesterday}
        })
        
        # Get referral statistics
        referred_count = await db.database.wallets.count_documents({
            "referred_by": {"$ne": None}
        })
        
        # Get top referrers
        top_referrers = await db.database.wallets.find(
            {"referral_count": {"$gt": 0}},
            {"wallet_address": 1, "referral_code": 1, "referral_count": 1}
        ).sort("referral_count", -1).limit(5).to_list(length=5)
        
        return {
            "total_registered_wallets": total_wallets,
            "registrations_today": recent_count,
            "wallets_with_referral": referred_count,
            "referral_percentage": round((referred_count / total_wallets * 100), 2) if total_wallets > 0 else 0,
            "top_referrers": top_referrers,
            "timestamp": datetime.utcnow()
        }
    except Exception as e:
        logger.error(f"Error getting stats: {e}")
        raise HTTPException(status_code=500, detail="Failed to get statistics")

@app.get("/referral/{referral_code}")
async def get_referral_info(referral_code: str):
    """Get referral information"""
    try:
        wallet = await db.database.wallets.find_one(
            {"referral_code": referral_code},
            {"wallet_address": 1, "referral_code": 1, "referral_count": 1, "created_at": 1}
        )
        
        if not wallet:
            raise HTTPException(status_code=404, detail="Referral code not found")
        
        # Get referred users
        referred_users = await db.database.wallets.find(
            {"referred_by": referral_code},
            {"wallet_address": 1, "created_at": 1}
        ).sort("created_at", -1).to_list(length=100)
        
        return {
            "referral_code": referral_code,
            "wallet_address": wallet["wallet_address"],
            "referral_count": wallet.get("referral_count", 0),
            "created_at": wallet["created_at"],
            "referred_users": referred_users
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting referral info: {e}")
        raise HTTPException(status_code=500, detail="Failed to get referral information")

if __name__ == "__main__":
    import uvicorn
    
    # Set environment variables if not set
    if not os.getenv("MONGODB_URL"):
        os.environ["MONGODB_URL"] = "mongodb+srv://profesor:root@cluster0.6kpph5n.mongodb.net/"
    if not os.getenv("MONGODB_DB_NAME"):
        os.environ["MONGODB_DB_NAME"] = "wallet_app"
    
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=8000,
        reload=True,
        log_level="info"
    )
