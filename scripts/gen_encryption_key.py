"""Prints a fresh Fernet key for ENCRYPTION_KEY in .env. Run once per environment."""
from cryptography.fernet import Fernet

if __name__ == "__main__":
    print(Fernet.generate_key().decode())
