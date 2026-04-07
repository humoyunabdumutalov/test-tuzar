FROM python:3.10-slim

# Ishchi muhit
WORKDIR /app

# Kutubxonalarni o'rnatish
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Kodlarni ko'chirish
COPY . .

# Botni ishga tushirish
CMD ["python", "main.py"]
