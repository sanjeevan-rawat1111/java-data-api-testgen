FROM node:20-slim
WORKDIR /runner
COPY package.json .
RUN npm install
COPY src/ ./src/
EXPOSE 3000
CMD ["node", "src/cli.js", "--all"]
