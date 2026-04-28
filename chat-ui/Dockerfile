FROM node:20-alpine

WORKDIR /app

# Install dependencies first (layer cache)
COPY package.json package-lock.json ./
RUN npm install

# Copy source
COPY . .

EXPOSE 5173

CMD ["npm", "run", "dev"]
