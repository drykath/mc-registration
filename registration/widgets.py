from django.forms.widgets import ChoiceWidget

class BootstrapChoiceWidget(ChoiceWidget):
    input_type = 'radio'
    template_name = 'registration/forms/widgets/buttongroup.html'
    option_template_name = 'registration/forms/widgets/buttongroup_option.html'
    disabled_options = None

    def disable_option(self, value, message):
        if not self.disabled_options:
            self.disabled_options = {}
        self.disabled_options[value] = message

    def create_option(self, name, value, label, selected, index, subindex=None, attrs=None):
        option = super().create_option(name, value, label, selected, index, subindex, attrs)
        if self.disabled_options and value.value in self.disabled_options:
            option['attrs']['disabled'] = True
            if self.disabled_options[value.value]:
                option['label'] += ' ({})'.format(self.disabled_options[value.value])
        return option
