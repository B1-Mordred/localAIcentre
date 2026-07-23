FROM node:22.18.0-bookworm-slim AS build

WORKDIR /app
ARG VITE_B1_API_BASE=https://api.ai.b1.germering
ENV VITE_B1_API_BASE=${VITE_B1_API_BASE}
COPY package.json package-lock.json ./
RUN npm ci
COPY index.html tsconfig.json vite.config.ts ./
COPY src ./src
RUN npm run build

FROM nginx:1.29.1-alpine
RUN apk upgrade --no-cache
COPY nginx.conf /etc/nginx/nginx.conf
COPY --from=build /app/dist /usr/share/nginx/html
USER nginx
EXPOSE 8080
