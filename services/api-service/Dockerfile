FROM eclipse-temurin:21-jdk-jammy AS build

WORKDIR /workspace

COPY gradlew build.gradle settings.gradle ./
COPY gradle ./gradle
COPY src ./src

RUN chmod +x gradlew && \
    ./gradlew --no-daemon clean bootJar && \
    cp "$(find build/libs -maxdepth 1 -name '*.jar' ! -name '*-plain.jar' | head -n 1)" build/app.jar

FROM eclipse-temurin:21-jre-jammy AS runtime

WORKDIR /app

COPY --from=build /workspace/build/app.jar /app/app.jar

EXPOSE 8080

ENV JAVA_TOOL_OPTIONS="-XX:MaxRAMPercentage=75.0 -Djava.security.egd=file:/dev/./urandom"

ENTRYPOINT ["java", "-jar", "/app/app.jar"]
