// 공통 UI 헬퍼
document.addEventListener("DOMContentLoaded", function () {
  // 카테고리 칩 선택 스타일
  document.querySelectorAll(".category-chip").forEach(function (chip) {
    var input = chip.querySelector("input");
    if (!input) return;
    chip.addEventListener("click", function () {
      document.querySelectorAll(".category-chip").forEach(function (c) {
        c.classList.remove("active");
      });
      input.checked = true;
      chip.classList.add("active");
    });
  });
});
