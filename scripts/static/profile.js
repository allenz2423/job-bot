(() => {
  "use strict";

  const editableControlSelector = [
    'input:not([type="hidden"]):not([type="button"]):not([type="submit"]):not([type="reset"]):not([type="image"]):not([disabled]):not([readonly])',
    "textarea:not([disabled]):not([readonly])",
    "select:not([disabled])",
    '[contenteditable="true"]:not([aria-disabled="true"])',
  ].join(",");

  function getRecordCard(template) {
    const content = template.content || template;
    if (content.matches?.(".record-card")) {
      return content;
    }
    return content.querySelector?.(".record-card") || null;
  }

  function addRecord(button) {
    const listId = button.dataset.addRecord;
    const templateId = button.dataset.templateId;
    if (!listId || !templateId) {
      return;
    }

    const list = document.getElementById(listId);
    const template = document.getElementById(templateId);
    if (!list || !template) {
      return;
    }

    const nextIndex = Number(list.dataset.nextIndex);
    if (!Number.isInteger(nextIndex)) {
      return;
    }

    const sourceCard = getRecordCard(template);
    if (!sourceCard) {
      return;
    }

    const card = sourceCard.cloneNode(true);
    const indexedElements = [card, ...card.querySelectorAll("[name], [id], [for]")];
    for (const element of indexedElements) {
      for (const attribute of ["name", "id", "for"]) {
        const value = element.getAttribute(attribute);
        if (value !== null && value.includes("__INDEX__")) {
          element.setAttribute(attribute, value.replace(/__INDEX__/g, String(nextIndex)));
        }
      }
    }

    list.dataset.nextIndex = String(nextIndex + 1);
    list.append(card);
    card.querySelector(editableControlSelector)?.focus();
  }

  function updateResumeFields(select) {
    const targeted = select.value === "targeted";
    const targetId = select.dataset.targetInput;
    const target = targetId ? document.getElementById(targetId) : null;
    if (target) {
      target.required = targeted;
      const group = target.closest("[data-resume-target-group]");
      if (group) group.hidden = !targeted;
    }
    select.closest("form")?.querySelectorAll("[data-primary-only]").forEach((label) => {
      label.hidden = targeted;
      const checkbox = label.querySelector("input");
      if (checkbox) checkbox.disabled = targeted;
    });
  }

  document.querySelectorAll("select[data-resume-purpose]").forEach((select) => {
    select.addEventListener("change", () => updateResumeFields(select));
    updateResumeFields(select);
  });

  document.addEventListener("click", (event) => {
    const addButton = event.target.closest?.("button[data-add-record]");
    if (addButton) {
      event.preventDefault();
      addRecord(addButton);
      return;
    }

    const removeButton = event.target.closest?.("button[data-remove-record]");
    if (removeButton) {
      event.preventDefault();
      removeButton.closest(".record-card")?.remove();
    }
  });
})();
