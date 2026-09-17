"""Bound multipart input before Starlette spools file data to disk."""

from fastapi import HTTPException, Request
from fastapi.routing import APIRoute
from starlette.formparsers import MultiPartException, MultiPartParser

from app.services.dataset_store import MAX_UPLOAD_BYTES


MULTIPART_OVERHEAD_BYTES = 64 * 1024


class LimitedMultiPartParser(MultiPartParser):
    complete = False

    def on_end(self) -> None:
        self.complete = True
        super().on_end()

    def on_part_begin(self) -> None:
        super().on_part_begin()
        self.file_bytes = 0

    def on_part_data(self, data: bytes, start: int, end: int) -> None:
        if self._current_part.file is not None:
            self.file_bytes += end - start
            if self.file_bytes > MAX_UPLOAD_BYTES:
                # Raise before the parent queues these bytes for UploadFile.write.
                raise MultiPartException("Файл превышает ограничение 25 МБ")
        super().on_part_data(data, start, end)


class LimitedUploadRoute(APIRoute):
    def get_route_handler(self):
        original_handler = super().get_route_handler()

        async def handler(request: Request):
            max_body = MAX_UPLOAD_BYTES + MULTIPART_OVERHEAD_BYTES
            length = request.headers.get("content-length")
            if length is not None:
                if not length.isascii() or not length.isdecimal():
                    raise HTTPException(400, "Некорректный размер тела запроса")
                if len(length) > 20 or int(length) > max_body:
                    raise HTTPException(400, "Тело запроса превышает допустимый размер")

            async def bounded_stream():
                received = 0
                async for chunk in request.stream():
                    received += len(chunk)
                    if received > max_body:
                        raise MultiPartException("Тело запроса превышает допустимый размер")
                    yield chunk

            if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "multipart/form-data":
                raise HTTPException(400, "Ожидается multipart/form-data с Excel-файлом")
            parser = LimitedMultiPartParser(
                request.headers, bounded_stream(), max_files=1, max_fields=0,
            )
            try:
                try:
                    form = await parser.parse()
                    if not parser.complete:
                        raise MultiPartException("Незавершённая multipart-загрузка")
                except MultiPartException as exc:
                    raise HTTPException(400, exc.message) from exc
                request._form = form
                return await original_handler(request)
            finally:
                # Also covers disconnects and parse errors on older Starlette.
                for file in parser._files_to_close_on_error:
                    file.close()

        return handler
